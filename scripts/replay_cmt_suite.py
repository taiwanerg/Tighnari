"""Run a frozen CMT cohort from Tighnari locations or UTC waveform window seeds."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import re
import subprocess

from obspy import Stream, UTCDateTime, read, read_inventory
from obspy.clients.fdsn import Client

from tighnari_seisloc.acquisition.cmt import EARTHSCOPE
from tighnari_seisloc.acquisition.cmt_greens import GreenStores
from tighnari_seisloc.acquisition.fdsn import calibrated_records, save_json
from tighnari_seisloc.acquisition.wphase import replay as replay_wphase
from tighnari_seisloc.config import Settings
from tighnari_seisloc.processing.cmt import ENSEMBLE_VERSION, VERSION
from tighnari_seisloc.processing.cmt_centroid import VERSION as CENTROID_VERSION
from tighnari_seisloc.processing.cmt_moment import VERSION as MOMENT_VERSION
from tighnari_seisloc.processing.focal import first_motions
from tighnari_seisloc.processing.pipeline import analyze as locate
from tighnari_seisloc.processing.travel import Iasp91
from tighnari_seisloc.services.regional_cmt import analyze

LOG = logging.getLogger(__name__)


def localization_archive(seed, target):
    """Acquire across the same network for every seed, independently of reference locations."""
    start, end = seed - 225, seed + 645
    xml, wave = target / "locator.xml", target / "locator.mseed"
    if xml.is_file() and wave.is_file():
        return read(str(wave)), read_inventory(str(xml))
    client = Client(EARTHSCOPE, timeout=20)
    if xml.is_file():
        inventory = read_inventory(str(xml))
    else:
        inventory = client.get_stations(
            network="TW", channel="BH?,HH?,EH?", starttime=start, endtime=end, level="response"
        )
        xml_tmp = xml.with_suffix(".xml.tmp")
        inventory.write(str(xml_tmp), format="STATIONXML")
        xml_tmp.replace(xml)
    stations = sorted({(n.code, s.code) for n in inventory for s in n})
    stream = Stream()
    for offset in range(0, len(stations), 12):
        try:
            stream += client.get_waveforms_bulk(
                [(n, s, "*", "BH?,HH?,EH?", start, end) for n, s in stations[offset : offset + 12]]
            )
        except Exception as exc:
            LOG.info("Locator archive group unavailable: %s", type(exc).__name__)
    if not stream:
        raise OSError("No independent regional waveforms")
    wave_tmp = wave.with_suffix(".mseed.tmp")
    stream.write(str(wave_tmp), format="MSEED")
    wave_tmp.replace(wave)
    return stream, inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=1)
    methods = parser.add_mutually_exclusive_group()
    methods.add_argument("--ensemble", action="store_true")
    methods.add_argument("--moment", action="store_true")
    methods.add_argument("--centroid", action="store_true")
    parser.add_argument("--wphase-python", type=Path)
    parser.add_argument("--wphase-greens", type=Path)
    parser.add_argument(
        "--wphase-fdsn-server", help="Fetch long-period data for cases marked wphase_fdsn"
    )
    parser.add_argument(
        "--download-cmt",
        action="store_true",
        help="Fetch further regional coverage in addition to the locator archive",
    )
    args = parser.parse_args()
    if bool(args.wphase_python) != bool(args.wphase_greens) or (
        args.wphase_fdsn_server and not args.wphase_python
    ):
        parser.error("W-phase replay requires both --wphase-python and --wphase-greens")
    if args.wphase_greens and not args.wphase_greens.exists():
        parser.error("The separate GA W-phase Green function database is required")
    settings = Settings.read()
    method = (
        CENTROID_VERSION
        if args.centroid
        else MOMENT_VERSION
        if args.moment
        else ENSEMBLE_VERSION
        if args.ensemble
        else VERSION
    )
    greens_path = args.store or settings.root / settings.regional_cmt_store
    model_path = settings.root / "runtime/iasp91.npz"
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.cohort.read_text(encoding="utf-8"))["cases"]
    ids = [row["id"] for row in cases]
    if len(ids) != len(set(ids)) or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", i) for i in ids
    ):
        parser.error("Case IDs must be unique safe path names")

    if any(
        not row.get("event") and not (args.output / row["id"] / "event.json").is_file()
        for row in cases
    ):
        from tighnari_seisloc.processing.picking import model_for

        model_for("original")

    def run(case):
        greens = GreenStores(greens_path)
        target = args.output / case["id"]
        target.mkdir(exist_ok=True)
        result_file = target / "result.json"
        event_file = target / "event.json"
        origin_status = "origin_unavailable"
        try:
            if result_file.is_file():
                saved = json.loads(result_file.read_text())
                if (
                    saved.get("method") == method
                    and saved.get("green_version") == greens.version
                    and saved.get("status") != "failed"
                ):
                    return
            inputs = []
            if case.get("event"):
                event = json.loads((args.cohort.parent / case["event"]).read_text())
            elif event_file.is_file():
                event = json.loads(event_file.read_text())
                inputs = [("earthscope", target / "locator.mseed", target / "locator.xml")]
            else:
                seed = UTCDateTime(case["window_center_utc"])
                stream, inv = localization_archive(seed, target)
                inputs = [("earthscope", target / "locator.mseed", target / "locator.xml")]
                stream.trim(seed - 60, seed + 180)
                records, _ = calibrated_records(stream, inv, "earthscope")
                report = locate(records, Iasp91(model_path), "phasenet", cache=target / "picks")
                candidates = [e for e in report["events"] if abs(e["origin"] - float(seed)) <= 20]
                if not candidates:
                    save_json(
                        result_file,
                        dict(
                            method=method,
                            status="insufficient_data",
                            reasons=["origin_unavailable"],
                            green_version=greens.version,
                            diagnostic_grid=greens.diagnostic_grid,
                        ),
                    )
                    print(case["id"], "insufficient_data", 0, flush=True)
                    return
                event = min(candidates, key=lambda e: (-e["count"], abs(e["origin"] - float(seed))))
                event["focal_mechanism"] = first_motions(
                    records, event, refine_onsets=True, stability=False
                )
                save_json(event_file, event)
            origin_status = "calculation_failed"
            inputs.extend(
                ("local", *(args.cohort.parent / p for p in pair))
                for pair in case.get("waveforms", [])
            )
            if event.get("focal_mechanism"):
                save_json(target / "initial.json", event["focal_mechanism"])
            result = analyze(
                event,
                greens,
                inputs,
                target,
                download=args.download_cmt,
                bats_url=settings.regional_cmt_bats_url,
                ensemble=args.ensemble,
                moment=args.moment,
                centroid=args.centroid,
            )
        except Exception as exc:
            result = dict(
                method=method,
                status="failed",
                reasons=[origin_status],
                error=type(exc).__name__,
                green_version=greens.version,
                diagnostic_grid=greens.diagnostic_grid,
            )
            save_json(result_file, result)
            LOG.exception("Case %s failed", case["id"])
        print(case["id"], result["status"], result.get("station_count", 0), flush=True)

    def run_case(case):
        run(case)
        if not args.wphase_python:
            return
        archive = case.get("wphase_archive")
        server = args.wphase_fdsn_server if case.get("wphase_fdsn") else None
        if not archive and not server:
            return
        target = args.output / case["id"]
        event_file = (
            args.cohort.parent / case["event"] if case.get("event") else target / "event.json"
        )
        if not event_file.is_file():
            return
        try:
            record = replay_wphase(
                event_file,
                args.wphase_greens,
                target,
                python=args.wphase_python,
                archive=[args.cohort.parent / p for p in archive] if archive else None,
                server=None if archive else server,
            )
            print(case["id"], "Mww", record["MomentTensor"]["drmag"], flush=True)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            LOG.exception("GA W-phase failed for %s", case["id"])

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run_case, cases))


if __name__ == "__main__":
    main()
