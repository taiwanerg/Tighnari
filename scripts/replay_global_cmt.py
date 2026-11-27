"""Replay a vertical long-period regional-independent CMT with a public Pyrocko store."""

import argparse
import json
from pathlib import Path

from tighnari_seisloc.acquisition.fdsn import save_json
from tighnari_seisloc.acquisition.global_cmt import (
    BAND,
    LONG_BAND,
    DELAYS,
    DEPTHS,
    DISTANCE,
    DT,
    TIMES,
    analyze,
    download,
    greens,
    prepare,
)

# Preserve the offline research helpers imported by existing replays.
__all__ = [
    "BAND",
    "LONG_BAND",
    "DELAYS",
    "DEPTHS",
    "DISTANCE",
    "DT",
    "TIMES",
    "analyze",
    "download",
    "greens",
    "prepare",
    "main",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", type=Path)
    parser.add_argument("store", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--centroid", action="store_true", help="Search a 3 x 3 horizontal grid")
    parser.add_argument("--long-band", action="store_true", help="Test 1.5–5 mHz sensitivity")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    event = json.loads(args.event.read_text())
    if args.download or args.download_only:
        download(event, args.output)
    if args.download_only:
        return
    band = LONG_BAND if args.long_band else BAND
    result = analyze(event, args.store, args.output, band=band, centroid=args.centroid)
    if result["status"] == "insufficient_data":
        parser.error(f"Only {result['station_count']} usable long-period stations")
    prefix = ("centroid-" if args.centroid else "") + ("long-" if args.long_band else "")
    filename = prefix + "result.json"
    save_json(args.output / filename, result)
    print(result["status"], result["station_count"], result.get("mw"), result.get("holdout_vr_pct"))


if __name__ == "__main__":
    main()
