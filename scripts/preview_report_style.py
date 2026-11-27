"""Render a subdued map preview from an existing event, without updating its data.

Run: PYTHONPATH=src .venv/bin/python scripts/preview_report_style.py
"""

import argparse
import json
from pathlib import Path

from tighnari_seisloc.services.reporting import render_map


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", nargs="?", default="runtime/miyako-comparison/event.json")
    parser.add_argument("--output", default="runtime/miyako-comparison/recommended-style.png")
    parser.add_argument("--travel-cache", default="runtime/iasp91.npz")
    args = parser.parse_args()
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    target = render_map(
        event,
        event.get("stations", []),
        args.output,
        travel_cache=args.travel_cache,
    )
    print(target)


if __name__ == "__main__":
    main()
