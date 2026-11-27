"""Compatibility namespace for 0.2; use tighnari_seisloc for new integrations."""

from pathlib import Path
import warnings
import tighnari_seisloc

warnings.warn("saiu_eew is deprecated; use tighnari_seisloc", DeprecationWarning, stacklevel=2)
__path__ = [str(Path(__file__).parent), str(Path(tighnari_seisloc.__file__).parent / "legacy")]
