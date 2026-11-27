"""Pinned official weights with bounded downloads and checksums."""

import hashlib
import importlib.metadata
import json
from pathlib import Path
import httpx


def setup_models(cache):
    root = Path(cache) / "models/v3/phasenet"
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for model, version in [("original", "2"), ("geofon", "2")]:
        for extension in ("json", "pt"):
            name = f"{model}.{extension}.v{version}"
            path = root / name
            if not path.exists():
                for base in (
                    "https://seisbench.gfz.de/mirror/",
                    "https://hifis-storage.desy.de/Helmholtz/HelmholtzAI/SeisBench/",
                ):
                    url = base + "models/v3/phasenet/" + name
                    try:
                        response = httpx.get(url, timeout=30, follow_redirects=True)
                        response.raise_for_status()
                        if not response.content:
                            raise ValueError("empty model")
                        tmp = path.with_suffix(path.suffix + ".download")
                        tmp.write_bytes(response.content)
                        tmp.replace(path)
                        print("downloaded", name, len(response.content), flush=True)
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        print(name, type(exc).__name__, flush=True)
                else:
                    raise SystemExit("Official model download failed")
            rows.append(
                dict(
                    file=name,
                    version=version,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    source="SeisBench official repository/mirror",
                )
            )
    (root.parents[2] / "provenance.json").write_text(
        json.dumps(dict(seisbench=importlib.metadata.version("seisbench"), files=rows), indent=2)
    )
