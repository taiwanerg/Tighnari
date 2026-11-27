"""Run the GA W-phase backend with this interpreter and a separate Green database."""

from pathlib import Path
from runpy import run_path

if __name__ == "__main__":
    backend = Path(__file__).resolve().parents[1] / "src/tighnari_seisloc/acquisition/wphase.py"
    run_path(str(backend), run_name="__main__")
