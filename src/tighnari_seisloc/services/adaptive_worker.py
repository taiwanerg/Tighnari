"""Run one adaptive CMT calculation in an isolated numerical environment."""

import json
from pathlib import Path
import sys

from ..acquisition.fdsn import save_json
from ..config import Settings
from ..processing import exclusions
from .adaptive_cmt import AdaptiveCMT


def main():
    request = json.load(sys.stdin)
    # The isolated environment does not read .env: the parent passes its rules.
    exclusions.configure(request.get("excluded_channels", ""))
    root = Path.cwd()
    settings = Settings(
        root,
        root / "runtime/events.sqlite3",
        None,
        regional_cmt_store=request["regional_store"],
        adaptive_global_store=request["global_store"],
    )
    worker = AdaptiveCMT(settings, None, None)
    inputs = [(provider, Path(wave), Path(xml)) for provider, wave, xml in request["inputs"]]
    result = worker._calculate(
        request["event"], request["kind"], Path(request["directory"]), inputs, request["stage"]
    )
    save_json(Path(sys.argv[1]), result)


if __name__ == "__main__":
    main()
