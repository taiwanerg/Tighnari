"""Run the public GA W-phase solver in its own Python environment."""

import argparse
from importlib.metadata import version
import json
from math import isclose, isfinite, log10, sqrt
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

RTP = ("rr", "tt", "pp", "rt", "rp", "tp")


def validate_solution(record):
    tensor, centroid = record.get("MomentTensor"), record.get("Centroid")
    if not tensor or not centroid or record.get("Error"):
        raise ValueError("GA W-phase did not resolve a centroid moment tensor")
    components = [tensor["tm" + key] for key in RTP]
    lat, lon, depth = (centroid[key] for key in ("latitude", "longitude", "depth"))
    moment, mw = tensor["scm"], tensor["drmag"]
    if (
        not all(isfinite(value) for value in (*components, lat, lon, depth, moment, mw))
        or not (-90 <= lat <= 90 and -360 <= lon <= 360 and depth >= 0 and moment > 0)
        or tensor["drmagt"].lower() != "mww"
    ):
        raise ValueError("Invalid GA W-phase centroid or moment tensor")
    calculated = sqrt(sum(x * x / 2 for x in components[:3]) + sum(x * x for x in components[3:]))
    if (
        not isclose(moment, calculated, rel_tol=1e-5)
        or abs(mw - 2 / 3 * (log10(moment) - 9.1)) > 0.01
    ):
        raise ValueError("GA W-phase moment and magnitude are inconsistent")


def replay(event, greens, output, *, python, archive=None, server=None, timeout=1800):
    """Run one solution and atomically replace the validated result on success."""
    if (archive is None) == (server is None):
        raise ValueError("Choose one W-phase archive or an FDSN server")
    event, greens, output = (Path(p).resolve() for p in (event, greens, output))
    if not greens.exists():
        raise FileNotFoundError(f"GA W-phase Green database is required at {greens}")
    output.mkdir(parents=True, exist_ok=True)
    # The independent interpreter must use its own NumPy/SciPy and GA dependencies.
    environment = {
        key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")
    }
    with TemporaryDirectory(prefix=".ga-wphase-", dir=output) as temporary:
        work = Path(temporary) / "run"
        command = [
            str(Path(python).absolute()),
            str(Path(__file__).resolve()),
            str(event),
            str(greens),
            str(work),
        ]
        if archive is not None:
            command.extend(["--archive", *(str(Path(p).resolve()) for p in archive)])
        else:
            command.extend(["--download", "--server", server])
        with (output / "ga-wphase.log").open("wb") as log:
            subprocess.run(
                command,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=timeout,
            )
        result = work / "ga-wphase.json"
        record = json.loads(result.read_text(encoding="utf-8"))
        validate_solution(record)
        result.replace(output / result.name)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", type=Path)
    parser.add_argument("greens", type=Path)
    parser.add_argument("output", type=Path)
    files = parser.add_mutually_exclusive_group(required=True)
    files.add_argument("--archive", nargs=2, type=Path, metavar=("MSEED", "STATIONXML"))
    files.add_argument("--download", action="store_true")
    parser.add_argument("--server", default="https://service.earthscope.org")
    args = parser.parse_args(argv)
    if not args.greens.exists():
        parser.error("The separate W-phase Green function database is required")

    from obspy import UTCDateTime, read, read_inventory
    from wphase import runwphase
    from wphase.psi.model import Event

    source = json.loads(args.event.read_text(encoding="utf-8"))
    eq = Event(
        id=source.get("id", args.event.stem),
        time=UTCDateTime(source["origin"]),
        latitude=source["lat"],
        longitude=source["lon"],
        depth=source["depth_km"],
    )
    wave, inventory = args.archive or (None, None)
    result = runwphase(
        output_dir=str(args.output.resolve()),
        output_dir_can_exist=True,
        greens_functions_dir=str(args.greens.resolve()),
        eqinfo=eq,
        inventory=read_inventory(str(inventory)) if inventory else None,
        waveforms=read(str(wave)) if wave else None,
        server=args.server if args.download else None,
        dist_range=[5, 90],
        use_only_z_components=True,
        processing_level=3,
        n_workers_in_pool=1,
        make_maps=False,
        make_plots=False,
        raise_errors=True,
    )
    record = json.loads(result.json())
    validate_solution(record)
    record["tighnari"] = dict(
        backend="ga-wphase",
        version=version("wphase"),
        location={key: source[key] for key in ("lat", "lon", "origin")},
        input_mode="fdsn" if args.download else "archive",
        green_database=args.greens.name,
    )
    temporary = args.output / "ga-wphase.json.tmp"
    temporary.write_text(json.dumps(record, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(args.output / "ga-wphase.json")


if __name__ == "__main__":
    main()
