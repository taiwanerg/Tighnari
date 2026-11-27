"""Configuration precedence: process environment, .env, then defaults."""

from dataclasses import dataclass
import os
import re
from pathlib import Path


def webhook_urls(value):
    """Accept one URL, comma/whitespace-separated URLs, or a sequence; preserve order."""
    values = [value] if isinstance(value, str) else (value or [])
    return tuple(dict.fromkeys(url for item in values for url in re.split(r"[,\s]+", item) if url))


def load_settings(root: Path | None = None) -> dict[str, str]:
    root = root or Path.cwd()
    local = {}
    if (root / ".env").exists():
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith(("TIGHNARI_", "SAIU_")):
                local[key] = value.strip().strip("\"'")
    keys = {
        k.split("_", 1)[1] for k in (*local, *os.environ) if k.startswith(("TIGHNARI_", "SAIU_"))
    }
    return {
        key: next(
            source[prefix + key]
            for source in (os.environ, local)
            for prefix in ("TIGHNARI_", "SAIU_")
            if prefix + key in source
        )
        for key in keys
    }


@dataclass
class Settings:
    root: Path
    database: Path
    webhook: str | tuple[str, ...] | None
    picker: str = "auto"
    max_stations: int = 60
    max_data_delay_s: float = 1800
    analysis_interval_s: float = 2
    live_mwp: bool = True
    bats_enabled: bool = False
    bats_url: str = "http://batsws.earth.sinica.edu.tw:8080"
    bats_delay_s: float = 900
    bats_poll_s: float = 60
    map_radius_km: float = 150
    waveform_retention_hours: float = 24
    waveform_archive_stations: int = 8
    public_url: str = ""

    gdms_enabled: bool = True
    gdms_delay_s: float = 900
    gdms_poll_s: float = 60

    gdms_auto_download: bool = False
    gdms_attention_webhook: str = ""
    gdms_mechanism_publish: bool = True
    gdms_schedule_jitter_s: float = 240

    regional_cmt_enabled: bool = False
    regional_cmt_publish: bool = False
    regional_cmt_store: str = "runtime/cmt-greens"
    regional_cmt_bats_url: str = ""
    focal_joint_shadow: bool = False
    regional_cmt_ensemble: bool = False
    regional_cmt_moment: bool = False
    adaptive_cmt_enabled: bool = False
    adaptive_global_min_magnitude: float = 6.0
    adaptive_global_store: str = "runtime/global-cmt/gf_stores/global_20s_shallow"
    adaptive_cmt_python: str = ""
    adaptive_wphase_python: str = ""
    adaptive_wphase_greens: str = ""
    # Large earthquakes can need ~17 min for the regional CAP/multiband search.
    adaptive_cmt_timeout_s: float = 2700
    # NET.STA.LOC.CHA rules; see processing/exclusions.py.
    excluded_channels: str = ""

    @classmethod
    def read(cls, root=None):
        root = Path(root or Path.cwd()).resolve()
        config = load_settings(root)
        os.environ.setdefault("MPLCONFIGDIR", str(root / "runtime/matplotlib"))
        os.environ.setdefault("SEISBENCH_CACHE_ROOT", str(root / "runtime/models"))
        return cls(
            root,
            root / config.get("DATABASE", "runtime/events.sqlite3"),
            config.get("DISCORD_WEBHOOK") or None,
            config.get("PICKER", "auto"),
            min(60, max(1, int(config.get("MAX_STATIONS", "60")))),
            max(60, min(7200, float(config.get("MAX_DATA_DELAY_S", "1800")))),
            max(1, min(30, float(config.get("ANALYSIS_INTERVAL_S", "2")))),
            config.get("LIVE_MWP", "true").lower() in ("1", "true", "yes"),
            config.get("BATS_ENABLED", "false").lower() in ("1", "true", "yes"),
            config.get("BATS_URL", "http://batsws.earth.sinica.edu.tw:8080").rstrip("/"),
            max(900, min(7200, float(config.get("BATS_DELAY_S", "900")))),
            max(30, min(3600, float(config.get("BATS_POLL_S", "60")))),
            max(25, min(500, float(config.get("MAP_RADIUS_KM", "150")))),
            max(1, float(config.get("WAVEFORM_RETENTION_HOURS", "24"))),
            min(8, max(1, int(config.get("WAVEFORM_ARCHIVE_STATIONS", "8")))),
            config.get("PUBLIC_URL", "").rstrip("/"),
            config.get("GDMS_ENABLED", "true").lower() in ("1", "true", "yes"),
            max(900, float(config.get("GDMS_DELAY_S", "900"))),
            max(60, min(3600, float(config.get("GDMS_POLL_S", "60")))),
            config.get("GDMS_AUTO_DOWNLOAD", "false").lower() in ("1", "true", "yes"),
            config.get("GDMS_ATTENTION_WEBHOOK", ""),
            config.get("GDMS_MECHANISM_PUBLISH", "true").lower() in ("1", "true", "yes"),
            max(0, min(240, float(config.get("GDMS_SCHEDULE_JITTER_S", "240")))),
            config.get("REGIONAL_CMT_ENABLED", "false").lower() in ("1", "true", "yes"),
            config.get("REGIONAL_CMT_PUBLISH", "false").lower() in ("1", "true", "yes"),
            config.get("REGIONAL_CMT_STORE", "runtime/cmt-greens"),
            config.get("REGIONAL_CMT_BATS_URL", "").rstrip("/"),
            config.get("FOCAL_JOINT_SHADOW", "false").lower() in ("1", "true", "yes"),
            config.get("REGIONAL_CMT_ENSEMBLE", "false").lower() in ("1", "true", "yes"),
            config.get("REGIONAL_CMT_MOMENT", "false").lower() in ("1", "true", "yes"),
            config.get("ADAPTIVE_CMT_ENABLED", "false").lower() in ("1", "true", "yes"),
            max(0, min(10, float(config.get("ADAPTIVE_GLOBAL_MIN_MAGNITUDE", "6.0")))),
            config.get("ADAPTIVE_GLOBAL_STORE", "runtime/global-cmt/gf_stores/global_20s_shallow"),
            config.get("ADAPTIVE_CMT_PYTHON", ""),
            config.get("ADAPTIVE_WPHASE_PYTHON", ""),
            config.get("ADAPTIVE_WPHASE_GREENS", ""),
            max(600, min(7200, float(config.get("ADAPTIVE_CMT_TIMEOUT_S", "2700")))),
            config.get("EXCLUDED_CHANNELS", ""),
        )
