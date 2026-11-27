"""Schedule revision-aware mechanism candidates for authenticated research review."""

import asyncio
from contextlib import closing
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import time

import numpy as np
from obspy.geodetics import gps2dist_azimuth

from ..acquisition import global_cmt, wphase
from ..acquisition.cmt import EARTHSCOPE, _download, gdms_inputs, prepare
from ..acquisition.cap import prepare_cap
from ..acquisition.cmt_greens import MODELS, SHORT_MODELS, GreenStores
from ..acquisition.cmt_research import prepare_components
from ..acquisition.fdsn import save_json
from ..processing.cap import DEFAULT_OFFSETS, DEPTHS as CAP_DEPTHS, invert_cap, profile_for
from ..processing.cap import PROFILES as CAP_PROFILES, fit_waveforms
from ..processing.cap import LIMITS as CAP_LIMITS, VERSION as CAP_VERSION
from ..processing.cmt import BAND, DEPTHS, DT, LIMITS, invert, kagan, rtp_to_ned
from ..processing.cmt_ensemble import (
    COMMON_PANEL,
    JACKKNIFE_MIN,
    MIN_POOL_SIZE,
    TRAIN_LIMIT,
    jackknife_validation,
    predict,
    split_stations,
    validation_scores,
)
from ..processing.cmt_research import phase_weights
from ..processing.focal import (
    POLARITY_CHECK_MIN,
    at_depth,
    depth_unconstrained,
    polarity_agreement,
    solve,
    tensor,
)
from .cap_plot import fit_image, fit_name
from .regional_cmt import PUBLIC_FIELDS, SCHEDULE, analyze
from .review import magnitude_priority_sql, review_of, visible_sql

LOG = logging.getLogger(__name__)
VERSION = "adaptive-mechanism-v4"
# Exit codes of a worker stopped by Ctrl+C / console close (Windows NTSTATUS, POSIX).
INTERRUPTED_CODES = {3221225786, -1073741510, 130, -2, -15, -9}
# A failure at one stage is retried once before it is kept until the next stage.
FAILURE_RETRIES = 1


class WorkerInterrupted(Exception):
    """The isolated calculation was stopped by shutdown, not by bad inputs."""


def kill_tree(process):
    """Stop the worker and every child (a venv launcher re-spawns the real Python)."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        else:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
        process.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def run_isolated(command, data, *, cwd, env, timeout, stopping=lambda: False, poll_s=5):
    """Run a worker with a deadline; on timeout or shutdown stop its whole tree."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        cwd=cwd,
        env=env,
        # POSIX: own process group so killpg reaches grandchildren.
        start_new_session=os.name != "nt",
    )
    try:
        try:
            process.stdin.write(data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while True:
            try:
                code = process.wait(timeout=poll_s)
                break
            except subprocess.TimeoutExpired:
                if stopping():
                    kill_tree(process)
                    raise WorkerInterrupted from None
                if time.monotonic() >= deadline:
                    kill_tree(process)
                    raise subprocess.TimeoutExpired(command, timeout) from None
    except BaseException:
        kill_tree(process)
        raise
    if code in INTERRUPTED_CODES:
        raise WorkerInterrupted
    if code:
        raise subprocess.CalledProcessError(code, command)


GLOBAL_SCHEDULE = (120, max(2700, global_cmt.WAVEFORM_READY_AGE), 3600, 7200)
SCHEDULES = {"regional": SCHEDULE, "global": GLOBAL_SCHEDULE, "wphase": GLOBAL_SCHEDULE[1:]}
MAX_AGE = 86400
CONFLICT_ANGLE = 35
SAME_PANEL_MARGIN = 10
SHORT_REGIONAL_BAND = (0.035, 0.085)
# Relative amplitude weight of GDMS stations added to the IRIS long-period fit.
# Equal weight (1.0) passed 3 rather than 5 of 9 medium events in the
# 2026-11-24 replay with unchanged accuracy, so the long-period path keeps the
# lower weight. The short-period CAP path weights GDMS and IRIS equally.
GDMS_WEIGHT = np.sqrt(0.5)
# Longer-period CAP profiles tried after the magnitude profile finds no passing fit.
# On the 2026-11-24 all-profile replay of 54 archived events (RMT, AutoBATS, USGS
# and AEHD references; runtime/cap-validation-20261124/profiles) one step raised
# passing events from 39 to 44 (2 of the 5 in the blind split), kept the median
# Kagan angle at 15 deg (P90 28.6 -> 27.7) and added no fit beyond 60 deg. A second
# step changed nothing on that set.
CAP_FALLBACK_STEPS = 1
# Research fields shown in /hoshi beyond the public regional CMT fields.
RESEARCH_FIELDS = (
    "profile",
    "pnl_band_hz",
    "fallback_from",
    "polarity_ray_depth_km",
    "quality_grade",
    "ray_depth_km",
    "solve_status",
    "representative_kagan_deg",
    "tensor_spread_deg",
    "misfit",
    "fit_image",
    "band_kagan_deg",
    "band_mw",
    "sensitivity_method",
    "method_version",
    "green_version",
    "diagnostic_grid",
)
# From this magnitude (ML or Mwp) the teleseismic W-phase-window CMT is preferred:
# regional fits of the four M6+ replay events missed RMT and AutoBATS by 30-80 deg,
# while qualified global fits agreed with USGS Mww within 13 deg.
GLOBAL_PRIMARY_MAGNITUDE = 6.0


def event_geometry(event):
    return {k: event.get(k) for k in ("origin", "lat", "lon", "depth_km")}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def magnitude(event):
    ml = event.get("magnitude")
    values = [
        ml,
        *(m.get("value") for m in event.get("magnitudes") or [] if m.get("type") == "Mwp"),
    ]
    return max((v for v in values if isinstance(v, (int, float)) and math.isfinite(v)), default=0)


def gap(stations):
    az = sorted(s["azimuth"] % 360 for s in stations)
    return max(b - a for a, b in zip(az, [*az[1:], az[0] + 360])) if az else 360


def add_sites(base, extra, holdout):
    """Retain original stations and reserve the same independent panel for both inversions."""
    kept = []
    for index, site in enumerate(extra):
        if any(
            site["id"] == original["id"]
            or gps2dist_azimuth(site["lat"], site["lon"], original["lat"], original["lon"])[0]
            < 2000
            for original in [*base, *holdout]
        ):
            continue
        kept.append(index)
    selected = []
    coverage = list(base)
    while kept and len(coverage) < TRAIN_LIMIT:
        ix = min(
            kept,
            key=lambda i: (gap([*coverage, extra[i]]), extra[i]["distance_km"], extra[i]["id"]),
        )
        selected.append(ix)
        coverage.append(extra[ix])
        kept.remove(ix)
    return selected


def stretch_stations(bank, factors):
    return bank * factors[None, :, None, None, None]


def check_panel(result, banks, count, panel, observed, mask=None):
    if not result.get("tensor_ned_nm"):
        return
    reasons = result["reasons"]
    if panel:
        modeled = predict(banks[result["model"]][:, count:], result, DEPTHS, DT)
        scores = validation_scores(observed, modeled, panel, range(len(panel)), mask=mask)
        values = [s["vr_pct"] for s in scores if s["vr_pct"] is not None]
        if len(values) == len(panel):
            result["validation_vr_pct"] = float(np.median(values))
        if result.get("validation_vr_pct", -math.inf) < LIMITS["holdout_vr"]:
            reasons.append("common_validation_fit")
        result["validation_method"] = COMMON_PANEL
    elif count >= JACKKNIFE_MIN:
        jackknife_validation(result)
    else:
        reasons.append("validation_geometry")
    if reasons and result["status"] == "experimental":
        result["status"] = "unconstrained"


def regional_candidates(event, greens, iris, gdms, *, phase_windows=False):
    """Fit capped IRIS and anchored GDMS variants with a shared IRIS holdout when possible."""
    sites, data, iris_audit = prepare(event, iris, station_limit=TRAIN_LIMIT)
    extra, supplemental, supplemental_mask, gdms_audit = prepare_components(
        event, gdms, BAND, "DISP", station_limit=TRAIN_LIMIT
    )
    training, holdout = split_stations(sites) if len(sites) >= MIN_POOL_SIZE else ((), ())
    fit = list(training) if holdout else list(range(len(sites)))
    retained = [sites[i] for i in fit]
    panel = [sites[i] for i in holdout]
    additions = add_sites(retained, extra, panel) if retained else []
    outcomes = []
    profiles = [("iris_cap", []), ("iris_gdms_anchored", additions)]
    if phase_windows and holdout:
        profiles.append(("regional_phase_windows", []))
    for name, indices in profiles:
        separate_phases = name == "regional_phase_windows"
        if separate_phases and any(row["status"] == "experimental" for row in outcomes):
            continue
        if name == "iris_gdms_anchored" and not indices:
            outcomes.append(
                dict(
                    id=name,
                    status="waiting",
                    station_count=len(retained),
                    reasons=["no_additional_gdms_stations"],
                )
            )
            continue
        stations = retained + [extra[i] for i in indices]
        if len(stations) < LIMITS["stations"]:
            outcomes.append(
                dict(
                    id=name,
                    status="insufficient_data",
                    station_count=len(stations),
                    reasons=["too_few_stations"],
                )
            )
            continue
        samples = np.concatenate([data[fit], supplemental[indices]]) if indices else data[fit]
        weights = None
        if indices or separate_phases:
            iris_mask = np.zeros_like(data[fit])
            for index, station in enumerate(retained):
                lo, hi = station["window"]
                iris_mask[index, :, lo:hi] = 1
            weights = (
                phase_weights(retained, iris_mask)
                if separate_phases
                else np.concatenate([iris_mask, supplemental_mask[indices]])
            )
        count = len(stations)
        banks = dict(greens.banks(event, stations + panel))
        factors = np.ones(count)
        factors[len(retained) :] = GDMS_WEIGHT
        training_banks = {
            model: stretch_stations(bank[:, :count], factors) for model, bank in banks.items()
        }
        result, _ = invert(
            samples * factors[:, None, None],
            training_banks,
            stations,
            energy_cap=3,
            sample_weights=weights,
        )
        check_panel(result, banks, count, panel, data[list(holdout)])
        result.update(
            id=name,
            method=name,
            validation_stations=[s["id"] for s in panel],
            supplemental_station_count=len(indices),
        )
        outcomes.append(result)
    return outcomes, dict(iris=iris_audit, gdms=gdms_audit)


def short_regional_candidate(event, greens, inputs):
    """Recover shorter-period responses and fit only the observed components."""
    name = "regional_short"
    sites, data, mask, audit = prepare_components(
        event, inputs, SHORT_REGIONAL_BAND, "DISP", station_limit=TRAIN_LIMIT
    )
    if len(sites) < LIMITS["stations"]:
        return dict(
            id=name,
            status="insufficient_data",
            station_count=len(sites),
            reasons=["too_few_stations"],
            band_hz=list(SHORT_REGIONAL_BAND),
        ), audit
    training, holdout = split_stations(sites) if len(sites) >= MIN_POOL_SIZE else ((), ())
    fit = list(training) if holdout else list(range(len(sites)))
    validation = list(holdout)
    stations = [sites[i] for i in fit]
    panel = [sites[i] for i in validation]
    count = len(stations)
    banks = dict(greens.banks(event, stations + panel, band=SHORT_REGIONAL_BAND))
    result, _ = invert(
        data[fit],
        {model: bank[:, :count] for model, bank in banks.items()},
        stations,
        band=SHORT_REGIONAL_BAND,
        energy_cap=3,
        sample_weights=mask[fit],
    )
    check_panel(result, banks, count, panel, data[validation], mask[validation])
    result.update(id=name, method=name, validation_stations=[s["id"] for s in panel])
    return result, audit


# Candidates built from the same polarities cannot test themselves against them.
POLARITY_SOURCES = (
    "first_motion",
    "first_motion_centroid",
    "joint",
    "joint_sh",
    "regional_cap_joint",
)


def first_motion_observations(event):
    """Return first motions computed at the event's current hypocenter."""
    focal = event.get("focal_mechanism") or {}
    if focal.get("location") != event_geometry(event):
        return []
    return [
        row
        for row in focal.get("observations") or []
        if row.get("polarity") in (-1, 1)
        and all(isinstance(row.get(k), (int, float)) for k in ("azimuth", "takeoff"))
    ]


def _cap_profile(event, greens, inputs, profile, observations):
    stations, data, masks, audit = prepare_cap(event, inputs, profile)
    base = dict(
        band_hz=list(profile["surface"]), pnl_band_hz=list(profile["pnl"]), profile=profile["name"]
    )
    if len(stations) < CAP_LIMITS["stations"]:
        row = dict(
            base,
            id="regional_cap",
            method="regional_cap",
            status="insufficient_data",
            station_count=len(stations),
            reasons=["too_few_stations"],
        )
        return [row], audit
    banks = {}
    for key, band in (("pnl", profile["pnl"]), ("surface", profile["surface"])):
        for model, bank in greens.banks(event, stations, CAP_DEPTHS, band=band):
            banks.setdefault(model, {})[key] = bank
    bands = dict(pnl=profile["pnl"], surface=profile["surface"])
    rows = []
    for name in ("regional_cap", "regional_cap_joint"):
        polarities = None
        ray_depth = None
        if name == "regional_cap_joint":
            polarities = observations
            free = rows[0]
            # Rays from an unconstrained hypocentral depth (often pinned at the surface
            # offshore) misplace polarities; use the waveform-only centroid instead.
            if (
                polarities
                and depth_unconstrained(event)
                and free.get("centroid_depth_km") is not None
            ):
                ray_depth = free["centroid_depth_km"]
                polarities = at_depth(polarities, event, ray_depth)
            if len(polarities or ()) < POLARITY_CHECK_MIN:
                continue
        result = invert_cap(
            data,
            masks,
            banks,
            stations,
            CAP_DEPTHS,
            bands=bands,
            observations=polarities,
            offsets=profile.get("offsets", DEFAULT_OFFSETS),
        )
        # The method version and Green store identify the release-gate record.
        result.update(
            id=name,
            method=name,
            profile=profile["name"],
            method_version=CAP_VERSION,
            green_version=getattr(greens, "version", None),
            diagnostic_grid=getattr(greens, "diagnostic_grid", True),
        )
        if ray_depth is not None:
            result["polarity_ray_depth_km"] = ray_depth
        result["waveform_fits"] = fit_waveforms(result, data, masks, banks, stations, CAP_DEPTHS)
        rows.append(result)
    return rows, audit


def cap_candidates(event, greens, inputs, *, fallback_steps=None):
    """Short-period CAP double couples, with a polarity-constrained variant.

    The magnitude profile runs first. If none of its fits passes, up to
    ``CAP_FALLBACK_STEPS`` longer-period profiles are tried in turn; their rows keep
    the CAP ids with a ``:profile`` suffix so all attempts stay visible.
    """
    steps = CAP_FALLBACK_STEPS if fallback_steps is None else fallback_steps
    observations = first_motion_observations(event)
    primary = profile_for(magnitude(event))
    names = [profile["name"] for profile in CAP_PROFILES]
    longer = CAP_PROFILES[names.index(primary["name"]) + 1 :] if primary["name"] in names else ()
    rows, audit = [], {}
    for step, profile in enumerate([primary, *longer][: 1 + steps]):
        part, part_audit = _cap_profile(event, greens, inputs, profile, observations)
        if step:
            for row in part:
                row.update(
                    id=f"{row['id']}:{profile['name']}",
                    fallback_from=primary["name"],
                )
            audit.setdefault("fallback", []).append(
                dict(profile=profile["name"], station_count=part_audit.get("station_count"))
            )
        else:
            audit.update(part_audit)
        rows.extend(part)
        if any(row["status"] == "experimental" for row in part):
            break
    return rows, audit


def candidate_view(row):
    fields = (*PUBLIC_FIELDS, *RESEARCH_FIELDS, "id", "supplemental_station_count")
    view = {k: row[k] for k in fields if k in row}
    view.pop("fit_stations", None)
    return view


def selection(rows, *, large=False):
    """Choose within one held-out panel, then compare directions across independent methods.

    With ``large`` the qualified global solution is ranked first; a regional solution
    that disagrees with it by more than CONFLICT_ANGLE still makes the event ambiguous.
    """
    qualified = []
    regional = []
    conflicts = []
    for row in rows:
        mt = row.get("tensor_ned_nm")
        if row.get("status") != "experimental" or row.get("reasons") or mt is None:
            continue
        if (row.get("first_motion_check") or {}).get("status") == "conflict":
            conflicts.append(row["id"])
            continue
        if row["id"] in ("iris_cap", "iris_gdms_anchored", "regional_phase_windows"):
            if row.get("validation_vr_pct") is not None:
                regional.append(row)
        else:
            qualified.append(row)
    if regional:
        top = max(r["validation_vr_pct"] for r in regional)
        qualified.extend(r for r in regional if top - r["validation_vr_pct"] <= SAME_PANEL_MARGIN)
    if not qualified:
        waiting = any(r["status"] == "waiting" for r in rows)
        state = dict(status="waiting" if waiting else "unconstrained", candidates=[])
        if conflicts:
            state["first_motion_conflicts"] = conflicts
        return state
    for i, row in enumerate(qualified):
        for other in qualified[:i]:
            angle = kagan(np.asarray(row["tensor_ned_nm"]), np.asarray(other["tensor_ned_nm"]))
            if angle is None or angle > CONFLICT_ANGLE:
                return dict(status="ambiguous", candidates=[r["id"] for r in qualified])
    # Cross-band VR values have different denominators; use a fixed priority.
    # Short-period CAP agreed best with RMT, AutoBATS and USGS in the 2026-11-24
    # held-out replay (median Kagan 11 deg against 18 deg for IRIS long period).
    # Choosing among near-optimal CAP fits by polarity lowered the median from
    # 18 to 12 deg on 10 events with first motions (6 better, 2 worse).
    order = {
        "regional_cap_joint": 0,
        "regional_cap": 1,
        "iris_cap": 2,
        "iris_gdms_anchored": 2,
        "regional_phase_windows": 2,
        "regional_multiband": 3,
        "regional_short": 4,
        "global": -1 if large else 5,
        "first_motion": 6,
    }
    chosen = min(
        qualified,
        key=lambda r: (order[r["id"].split(":", 1)[0]], -r.get("validation_vr_pct", 0), r["id"]),
    )
    state = dict(
        status="experimental", representative=chosen["id"], candidates=[r["id"] for r in qualified]
    )
    check = chosen.get("first_motion_check") or {}
    state["first_motion_corroborated"] = check.get("status") == "consistent" or any(
        r["id"] == "first_motion" for r in qualified if r is not chosen
    )
    if conflicts:
        state["first_motion_conflicts"] = conflicts
    return state


class AdaptiveCMT:
    def __init__(self, settings, store, publication_lock):
        self.settings, self.store, self.publication_lock = settings, store, publication_lock
        self.stopping = False
        self.greens = None
        self.greens_files = None
        self.cap_greens = None
        self.cap_greens_files = None
        self.running = {}
        self.centroid_cache = {}
        if settings.adaptive_cmt_enabled:
            with store.connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS adaptive_cmt_jobs (
                    event_id TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL,
                    stage INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (event_id, kind)
                )""")

    def kinds(self):
        if self.settings.adaptive_wphase_python and self.settings.adaptive_wphase_greens:
            return tuple(SCHEDULES)
        return ("regional", "global")

    def sources(self, event, kind):
        if kind != "regional":
            return []
        eid = event["id"]
        if eid in ("", ".", "..") or "/" in eid or "\\" in eid:
            return []
        return gdms_inputs(self.settings.root / "runtime/gdms/inbox" / eid)

    def other_large_events(self, event, kind):
        if kind == "regional":
            return []
        origin = event["origin"]
        if not isinstance(origin, (int, float)) or not math.isfinite(origin):
            return []
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT payload FROM events WHERE mode='live' AND status='located' AND id<>? "
                "AND origin BETWEEN ? AND ? AND " + visible_sql() + " ORDER BY origin,id",
                (
                    event["id"],
                    origin + float(global_cmt.TIMES[0]),
                    origin + float(global_cmt.TIMES[-1]),
                ),
            ).fetchall()
        events = [json.loads(row[0]) for row in rows]
        return [
            [other["id"], other["origin"], magnitude(other)]
            for other in events
            if magnitude(other) >= self.settings.adaptive_global_min_magnitude
        ]

    def green_files(self, kind):
        folder = self.settings.root / (
            self.settings.regional_cmt_store
            if kind == "regional"
            else self.settings.adaptive_global_store
        )
        if kind in ("regional", "cap"):
            files = [
                p
                for model in (MODELS if kind == "regional" else SHORT_MODELS)
                for p in (
                    folder / model / "config",
                    folder / model / "index",
                    folder / model / "traces",
                    folder / model / "extra/qseis",
                    folder / model / "cmt-model.json",
                )
            ]
        elif kind == "wphase":
            files = [
                self.settings.root / config
                for config in (
                    self.settings.adaptive_wphase_greens,
                    self.settings.adaptive_wphase_python,
                )
            ]
        else:
            files = [folder / name for name in ("config", "index", "traces")]
        version = []
        for path in files:
            stat = path.stat() if path.exists() else None
            version.append((str(path), stat.st_size, stat.st_mtime_ns) if stat else None)
        return version

    def signature(self, event, kind, inputs, other_events=None):
        inputs_version = []
        for provider, wave, xml in inputs:
            for path in (wave, xml):
                stat = path.stat()
                inputs_version.append((provider, path.name, stat.st_size, stat.st_mtime_ns))
        return fingerprint(
            [
                VERSION,
                kind,
                event_geometry(event),
                self.green_files(kind),
                self.green_files("cap") if kind == "regional" else None,
                inputs_version,
                self.settings.adaptive_cmt_python,
                self.other_large_events(event, kind) if other_events is None else other_events,
            ]
        )

    def _regional(self, event, directory, inputs, stage):
        store_path = self.settings.root / self.settings.regional_cmt_store
        try:
            files = self.green_files("regional")
            if self.greens is None or self.greens_files != files:
                self.greens = GreenStores(store_path)
                self.greens_files = files
        except (OSError, ImportError, ValueError):
            return dict(status="waiting", candidates=[], reasons=["waiting_for_regional_greens"])
        try:
            iris = _download(event, "earthscope", EARTHSCOPE, directory)
        except Exception:
            LOG.exception("Adaptive regional waveform download failed")
            iris = []
        rows, audit = regional_candidates(event, self.greens, iris, inputs, phase_windows=stage > 0)
        try:
            files = self.green_files("cap")
            if self.cap_greens is None or self.cap_greens_files != files:
                self.cap_greens = GreenStores(store_path, SHORT_MODELS)
                self.cap_greens_files = files
        except (OSError, ImportError, ValueError):
            self.cap_greens = None
        if self.cap_greens is None:
            rows.append(
                dict(
                    id="regional_cap",
                    status="waiting",
                    candidates=[],
                    reasons=["waiting_for_short_period_greens"],
                )
            )
        elif iris or inputs:
            cap_rows, audit["cap"] = cap_candidates(event, self.cap_greens, [*iris, *inputs])
            rows.extend(cap_rows)
        if not iris and stage < len(SCHEDULE) - 1:
            rows[0].update(status="waiting", reasons=["waiting_for_regional_waveforms"])
        if stage == len(SCHEDULE) - 1 and rows[1]["status"] == "waiting":
            rows[1]["status"] = "insufficient_data"
        if (iris or inputs) and not any(r["status"] == "experimental" for r in rows):
            broad = analyze(
                event, self.greens, [*iris, *inputs], directory / "multiband", moment=True
            )
            families = broad.get("solutions", []) if broad["status"] == "ambiguous" else []
            if families:
                rows.extend(
                    dict(row, id=f"regional_multiband:{row['id']}", method="regional_multiband")
                    for row in families
                )
            else:
                rows.append(dict(broad, id="regional_multiband", method="regional_multiband"))
            if stage > 0 and not any(
                row["status"] in ("experimental", "ambiguous") for row in rows
            ):
                short, audit["short_period"] = short_regional_candidate(
                    event, self.greens, [*iris, *inputs]
                )
                rows.append(short)
        for row in rows:
            fits = row.pop("waveform_fits", None)
            if fits is not None:
                row["fit_image"] = fit_image(
                    dict(row, waveform_fits=fits), directory / fit_name(row["id"])
                )
        results = [candidate_view(row) for row in rows]
        save_json(directory / "regional.json", dict(candidates=rows, provenance=audit))
        return dict(status="evaluated", candidates=results)

    def _global(self, event, directory, stage):
        if stage == 0:
            global_cmt.prefetch(event, directory)
            return dict(
                status="waiting", candidates=[], reasons=["waiting_for_long_period_waveforms"]
            )
        store_path = self.settings.root / self.settings.adaptive_global_store
        if not all((store_path / p).is_file() for p in ("config", "index", "traces")):
            return dict(status="waiting", candidates=[], reasons=["waiting_for_global_greens"])
        try:
            global_cmt.download(event, directory, reuse_inventory=True)
        except global_cmt.NoGlobalWaveforms:
            status = "waiting" if stage < len(GLOBAL_SCHEDULE) - 1 else "insufficient_data"
            return dict(status=status, candidates=[], reasons=["waiting_for_long_period_waveforms"])
        result = global_cmt.analyze(event, store_path, directory)
        long_result = None
        if result.get("tensor_ned_nm"):
            try:
                long_result = global_cmt.analyze(
                    event, store_path, directory, band=global_cmt.LONG_BAND
                )
            except ValueError:
                LOG.exception("Adaptive long-band global fit failed")
            global_cmt.qualify(result, long_result)
        result["id"] = "global"
        save_json(directory / "result.json", result)
        candidates = [candidate_view(result)]
        if long_result is not None and long_result.get("tensor_ned_nm"):
            save_json(directory / "long-result.json", long_result)
            candidates.append(
                candidate_view(dict(long_result, id="global_long", status="research_candidate"))
            )
        return dict(status="evaluated", candidates=candidates)

    def _calculate(self, event, kind, directory, inputs, stage):
        if kind == "wphase":
            return self._wphase(event, directory)
        if not self.settings.adaptive_cmt_python:
            if kind == "regional":
                return self._regional(event, directory, inputs, stage)
            return self._global(event, directory, stage)
        python = self.settings.root / self.settings.adaptive_cmt_python
        source = Path(__file__).resolve().parents[2]
        environment = {
            **os.environ,
            "PYTHONPATH": str(source),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MPLBACKEND": "Agg",
        }
        request = dict(
            event=event,
            kind=kind,
            directory=str(directory),
            stage=stage,
            inputs=[(provider, str(wave), str(xml)) for provider, wave, xml in inputs],
            regional_store=self.settings.regional_cmt_store,
            global_store=self.settings.adaptive_global_store,
            excluded_channels=self.settings.excluded_channels,
        )
        # Windows may briefly lock files of a killed worker; never fail on cleanup.
        with TemporaryDirectory(
            prefix="worker-", dir=directory, ignore_cleanup_errors=True
        ) as work:
            output = Path(work) / "result.json"
            run_isolated(
                [str(python), "-m", "tighnari_seisloc.services.adaptive_worker", str(output)],
                json.dumps(request, allow_nan=False).encode(),
                cwd=self.settings.root,
                env=environment,
                timeout=self.settings.adaptive_cmt_timeout_s,
                stopping=lambda: self.stopping,
            )
            return json.loads(output.read_text(encoding="utf-8"))

    def _wphase(self, event, directory):
        python = self.settings.root / self.settings.adaptive_wphase_python
        greens = self.settings.root / self.settings.adaptive_wphase_greens
        if not python.is_file() or not greens.exists():
            return dict(status="waiting", candidates=[], reasons=["waiting_for_wphase_setup"])
        seed = directory / "event.json"
        save_json(seed, event_geometry(event))
        result = wphase.replay(
            seed, greens, directory, python=python, server=global_cmt.SERVER, timeout=1200
        )
        mt = result["MomentTensor"]
        candidate = dict(
            id="wphase",
            method="ga-wphase",
            status="research_candidate",
            tensor_ned_nm=rtp_to_ned([mt["tm" + key] for key in wphase.RTP]).tolist(),
            mw=mt["drmag"],
            centroid_depth_km=result["Centroid"]["depth"],
            reasons=["wphase_independent_validation_pending"],
        )
        return dict(status="evaluated", candidates=[candidate])

    def _job(self, event, kind, now):
        schedule = SCHEDULES[kind]
        geo = event_geometry(event)
        if (
            event.get("summary_only")
            or event.get("region", "tw") != "tw"
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in geo.values())
        ):
            return False
        if kind != "regional" and magnitude(event) < self.settings.adaptive_global_min_magnitude:
            return False
        stages = [i for i, age in enumerate(schedule) if now - geo["origin"] >= age]
        if not stages:
            return False
        stage = stages[-1]
        eid = event["id"]
        inputs = self.sources(event, kind)
        other_events = self.other_large_events(event, kind)
        version = self.signature(event, kind, inputs, other_events)
        with self.store.connect() as db:
            prior = db.execute(
                "SELECT signature,stage,payload FROM adaptive_cmt_jobs WHERE event_id=? AND kind=?",
                (eid, kind),
            ).fetchone()
        if prior and prior["signature"] == version and prior["stage"] >= stage:
            return False
        previous = json.loads(prior["payload"]) if prior and prior["signature"] == version else {}
        if kind == "wphase" and prior and prior["signature"] == version:
            if json.loads(prior["payload"])["status"] == "evaluated":
                return False
        event_folder = hashlib.sha256(eid.encode()).hexdigest()
        directory = (
            self.settings.root / "runtime/adaptive-cmt" / event_folder / fingerprint(geo) / kind
        )
        directory.mkdir(parents=True, exist_ok=True)
        self.running[kind] = (eid, version)
        try:
            try:
                value = self._calculate(event, kind, directory, inputs, stage)
            except WorkerInterrupted:
                # Shutdown/Ctrl+C is not a scientific failure: save nothing, rerun later.
                LOG.warning(
                    "Adaptive %s calculation for %s was interrupted; it will rerun", kind, eid
                )
                return True
            except subprocess.TimeoutExpired:
                LOG.error("Adaptive %s calculation timed out for %s", kind, eid)
                value = dict(status="failed", candidates=[], reasons=["calculation_timeout"])
            except Exception:
                LOG.exception("Adaptive %s calculation failed for %s", kind, eid)
                value = dict(status="failed", candidates=[], reasons=["calculation_failed"])
            saved_stage = stage
            if value["status"] == "failed":
                failures = (
                    previous.get("failures", 0) + 1 if previous.get("status") == "failed" else 1
                )
                value["failures"] = failures
                if failures <= FAILURE_RETRIES:
                    # Below the reached stage, so the next tick retries this stage once.
                    saved_stage = stage - 1
            if other_events:
                for candidate in value["candidates"]:
                    if candidate.get("tensor_ned_nm"):
                        candidate["reasons"] = [
                            *candidate.get("reasons", []),
                            "overlapping_large_event_window",
                        ]
                        if candidate["status"] == "experimental":
                            candidate["status"] = "unconstrained"
            with self.publication_lock:
                current = self.store.get(eid)
                if (
                    self.stopping
                    or not current
                    or current.get("status") != "located"
                    or current.get("summary_only")
                    or review_of(current)["error"]
                    or (
                        kind != "regional"
                        and magnitude(current) < self.settings.adaptive_global_min_magnitude
                    )
                    or self.signature(current, kind, self.sources(current, kind)) != version
                ):
                    return True
                with self.store.connect() as db:
                    db.execute(
                        """INSERT INTO adaptive_cmt_jobs VALUES (?,?,?,?,?)
                        ON CONFLICT(event_id,kind) DO UPDATE SET signature=excluded.signature,
                        stage=excluded.stage,payload=excluded.payload""",
                        (
                            eid,
                            kind,
                            version,
                            saved_stage,
                            # The location lets a relocation show this result as "previous".
                            json.dumps(dict(value, location=geo), allow_nan=False),
                        ),
                    )
        finally:
            self.running.pop(kind, None)
        return True

    def diagnostics(self, event):
        if not self.settings.adaptive_cmt_enabled or event.get("summary_only"):
            return None
        eid = event["id"]
        with self.store.connect() as db:
            saved = {
                r["kind"]: r
                for r in db.execute("SELECT * FROM adaptive_cmt_jobs WHERE event_id=?", (eid,))
            }
        jobs, candidates, previous = {}, [], []
        for kind in self.kinds():
            if (
                kind != "regional"
                and magnitude(event) < self.settings.adaptive_global_min_magnitude
            ):
                continue
            try:
                version = self.signature(event, kind, self.sources(event, kind))
            except (OSError, ValueError, KeyError):
                version = None
            if kind in saved and saved[kind]["signature"] == version:
                job = json.loads(saved[kind]["payload"])
                if self.running.get(kind) == (eid, version):
                    job["status"] = "running"
            elif version is not None and self.running.get(kind) == (eid, version):
                job = dict(status="running", candidates=[])
            elif (
                event.get("status") == "located"
                and event.get("mode") == "live"
                and time.time() - event["origin"] <= MAX_AGE
            ):
                job = dict(status="waiting", candidates=[])
            else:
                continue
            if (
                job["status"] in ("waiting", "running")
                and kind in saved
                and saved[kind]["signature"] != version
            ):
                # Relocated or new inputs: keep showing the last finished result,
                # clearly marked, until the recalculation replaces it.
                earlier = json.loads(saved[kind]["payload"])
                # Only a moved hypocentre makes it "previous"; legacy rows lack the key.
                moved = earlier.get("location", None) != event_geometry(event)
                rows = [
                    dict(
                        {k: v for k, v in row.items() if k != "fit_image"},
                        id="previous:" + row["id"],
                        previous_of=row["id"],
                        previous_location=True,
                    )
                    for row in earlier.get("candidates", [])
                    if moved and earlier.get("status") == "evaluated"
                ]
                if rows:
                    job["previous_result"] = True
                    previous.extend(rows)
            jobs[kind] = job
            candidates.extend(job["candidates"])
        focal = event.get("focal_mechanism")
        if focal and focal.get("location", event_geometry(event)) == event_geometry(event):
            candidates.append(
                candidate_view(
                    dict(
                        focal,
                        id="first_motion",
                        method="first_motion",
                        tensor_ned_nm=focal.get("tensor_ned"),
                    )
                )
            )
            for kind in ("joint", "joint_sh"):
                row = (focal.get("regional_comparison") or {}).get(kind) or {}
                plane = row.get("plane")
                if plane:
                    validation = {
                        name: {
                            key: fold[key]
                            for key in (
                                "eligible_folds",
                                "attempted_folds",
                                "kagan_p90_deg",
                                "prediction",
                            )
                            if key in fold
                        }
                        for name, fold in row.get("cross_validation", {}).items()
                    }
                    candidates.append(
                        dict(
                            id=kind,
                            method=kind,
                            status="research_candidate",
                            tensor_ned_nm=tensor(*plane).tolist(),
                            station_count=row.get("ratio_count"),
                            reasons=row.get("reasons", []),
                            joint_validation=validation,
                        )
                    )
        for row in candidates:
            if row["id"].split(":", 1)[0] not in POLARITY_SOURCES and row.get("tensor_ned_nm"):
                check = polarity_agreement(
                    row["tensor_ned_nm"], focal, event, depth_km=row.get("centroid_depth_km")
                )
                if check is not None:
                    row["first_motion_check"] = check
        state = selection(candidates, large=magnitude(event) >= GLOBAL_PRIMARY_MAGNITUDE)
        centroid = self.centroid_first_motion(event, focal, candidates, state)
        if centroid is not None:
            candidates.append(centroid)
        if not state["candidates"] and any(
            job["status"] in ("waiting", "running") for job in jobs.values()
        ):
            state["status"] = "waiting"
        # Earlier results are listed after selection and never become the pick.
        candidates.extend(previous)
        return dict(scope="research", jobs=jobs, candidates=candidates, selection=state)

    def representative(self, event):
        """Research pick of ``event`` (or None) and whether no job is waiting or running."""
        result = self.diagnostics(event) or {}
        settled = not any(
            job["status"] in ("waiting", "running") for job in result.get("jobs", {}).values()
        )
        chosen = (result.get("selection") or {}).get("representative")
        row = next(
            (
                row
                for row in result.get("candidates", [])
                if row["id"] == chosen and row.get("tensor_ned_nm") is not None
            ),
            None,
        )
        return row, settled

    def fit_path(self, event, candidate_id):
        """Saved CAP fit image of the current regional job, or None."""
        folder = hashlib.sha256(event["id"].encode()).hexdigest()
        path = (
            self.settings.root
            / "runtime/adaptive-cmt"
            / folder
            / fingerprint(event_geometry(event))
            / "regional"
            / fit_name(candidate_id)
        )
        return path if path.is_file() else None

    def centroid_first_motion(self, event, focal, candidates, state):
        """Re-solve first motions at the waveform centroid when the located depth is free."""
        if (
            not depth_unconstrained(event)
            or state.get("status") != "experimental"
            or not focal
            or focal.get("location", event_geometry(event)) != event_geometry(event)
        ):
            return None
        chosen = next((r for r in candidates if r["id"] == state.get("representative")), None)
        depth = (chosen or {}).get("centroid_depth_km")
        if depth is None or not chosen.get("tensor_ned_nm"):
            return None
        observations = [
            row for row in focal.get("observations") or [] if row.get("polarity") in (-1, 1)
        ]
        key = fingerprint(
            [event_geometry(event), depth, [(r.get("id"), r["polarity"]) for r in observations]]
        )
        if key not in self.centroid_cache:
            result = solve(at_depth(observations, event, depth))
            row = dict(
                id="first_motion_centroid",
                method="first_motion_centroid",
                status="research_candidate",
                solve_status=result["status"],
                reasons=result.get("reasons", []),
                station_count=result.get("station_count"),
                ray_depth_km=float(depth),
            )
            if result.get("tensor_ned"):
                angle = kagan(np.asarray(result["tensor_ned"]), np.asarray(chosen["tensor_ned_nm"]))
                row.update(
                    tensor_ned_nm=result["tensor_ned"],
                    nodal_planes=[
                        [p["strike"], p["dip"], p["rake"]] for p in result["nodal_planes"]
                    ],
                    azimuth_gap_deg=result.get("azimuth_gap_deg"),
                    misfit=result.get("misfit"),
                    tensor_spread_deg=result.get("tensor_spread_deg"),
                    quality_grade=result.get("quality_grade"),
                    representative_kagan_deg=angle,
                )
            if len(self.centroid_cache) > 256:
                self.centroid_cache.clear()
            self.centroid_cache[key] = row
        return dict(self.centroid_cache[key])

    def tick(self, kind, now=None):
        if self.stopping or not self.settings.adaptive_cmt_enabled or kind not in self.kinds():
            return
        lease_path = self.settings.root / "runtime/adaptive-cmt" / f"{kind}.lock.sqlite3"
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        # Separate leases let regional and global work run in parallel.
        with closing(sqlite3.connect(lease_path, timeout=0)) as lease:
            try:
                lease.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    return
                raise
            self._tick(kind, time.time() if now is None else now)

    def _tick(self, kind, now):
        minimum_age = SCHEDULES[kind][0]
        with self.store.connect() as db:
            events = db.execute(
                "SELECT payload FROM events WHERE mode='live' AND status='located' AND "
                + visible_sql()
                + " AND origin BETWEEN ? AND ? ORDER BY "
                + magnitude_priority_sql()
                + ",origin",
                (now - MAX_AGE, now - minimum_age),
            ).fetchall()
        for row in events:
            try:
                if self.stopping or self._job(json.loads(row[0]), kind, now):
                    break
            except (OSError, ValueError, KeyError):
                LOG.exception("Adaptive %s inputs unavailable", kind)

    async def run(self, kind):
        while not self.stopping:
            try:
                await asyncio.to_thread(self.tick, kind)
            except Exception:
                LOG.exception("Adaptive %s worker failed", kind)
            await asyncio.sleep(60)
