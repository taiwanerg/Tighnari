"""Launch Tighnari-SeisLOC and the local observatory: python start.py."""

from pathlib import Path
import sys


def main():
    # Allows a source checkout to run once dependencies are installed.
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "src"))
    import os

    os.chdir(root)
    from tighnari_seisloc.cli import main as cli

    cli(["serve", *sys.argv[1:]])


if __name__ == "__main__":
    main()
