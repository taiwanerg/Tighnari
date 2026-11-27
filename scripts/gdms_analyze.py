# ruff: noqa: E402
"""Analyze a downloaded inbox in isolation, including experimental first motions."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obspy import UTCDateTime
from tighnari_seisloc.acquisition.gdms import acquire_gdms
from tighnari_seisloc.acquisition.fdsn import save_json
from tighnari_seisloc.processing.pipeline import analyze
from tighnari_seisloc.processing.focal import first_motions
from tighnari_seisloc.processing.travel import Iasp91
from tighnari_seisloc.services.waveforms import save_records, load_records
from tighnari_seisloc.services.mechanism_plot import mechanism_image
from tighnari_seisloc.services.enrichment import merge_records
from tighnari_seisloc.services.reporting import render_map
from tighnari_seisloc.services.storage import render_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox")
    parser.add_argument("--iris-records", help="Previously calibrated IRIS records NPZ")
    parser.add_argument("--origin", required=True, help="UTC ISO timestamp")
    parser.add_argument("--output", required=True)
    parser.add_argument("--pick-cache")
    parser.add_argument("--mwp-directory", help="Optional separate far-station archive/cache")
    args = parser.parse_args()
    if not args.inbox and not args.iris_records:
        parser.error("At least one of --inbox or --iris-records is required")
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    origin = float(UTCDateTime(args.origin))
    measurements, provenance = (
        acquire_gdms(origin - 60, origin + 240, args.inbox) if args.inbox else ([], {})
    )
    if args.iris_records:
        measurements += load_records(args.iris_records)
    records = merge_records([], measurements, origin, preferred_provider="gdms")
    save_records(records, out / "calibrated.npz")
    save_records(measurements, out / "measurements.npz")
    report = analyze(
        records,
        Iasp91("runtime/iasp91.npz"),
        "phasenet",
        cache=Path(args.pick_cache) if args.pick_cache else out / "picks",
        measurement_records=measurements,
    )
    save_json(out / "analysis.json", report)
    candidates = [e for e in report["events"] if abs(e["origin"] - origin) <= 30]
    candidates.sort(key=lambda e: (-e["count"], -e["phase_count"], e["rms_s"]))
    result = dict(
        station_count=len({r.id for r in records}),
        provenance=provenance,
        event_count=len(candidates),
        generative_ai=False,
    )
    if candidates:
        event = candidates[0]
        event.update(
            id=out.name, revision=1, mode="historical", travel_model=report["travel_model"]
        )
        event["focal_mechanism"] = first_motions(
            measurements, event, refine_onsets=True, stability=True
        )
        if args.mwp_directory:
            from tighnari_seisloc.acquisition.teleseismic import estimate

            mwp = estimate(event, args.mwp_directory)
            event["magnitudes"].append(mwp)
            result["mwp"] = {k: mwp.get(k) for k in ("value", "count", "reason", "uncertainty")}
        save_json(out / "event.json", event)
        mechanism_image(event, out / "mechanism.png")
        for view in ("overview", "closeup"):
            render_map(event, event["stations"], out / f"{view}.png", view=view)
        render_archive(records, event, out / "waveforms.png", limit=12)
        result["event"] = {
            k: event.get(k)
            for k in (
                "origin",
                "lat",
                "lon",
                "depth_km",
                "depth_status",
                "magnitude",
                "count",
                "phase_count",
                "rms_s",
                "gap_deg",
                "quality_grade",
            )
        }
        result["mechanism"] = {
            k: event["focal_mechanism"].get(k)
            for k in (
                "status",
                "station_count",
                "reasons",
                "misfit",
                "tensor_spread_deg",
                "stability",
            )
        }
    save_json(out / "summary.json", result)
    print(result)


if __name__ == "__main__":
    main()
