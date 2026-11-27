"""Local HTTP observatory. Acquisition and CPU work never run on the request loop."""

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
import gzip
import json
from functools import lru_cache
from pathlib import Path
import secrets
import time
import threading
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse
from fastapi.staticfiles import StaticFiles
from ..config import Settings, load_settings
from .http import (
    NegotiatedGZipMiddleware,
    accepts_gzip,
    cached_file,
    CachedStaticFiles,
    SharedTTLCache,
)
from .store import EventStore
from .seo import SITEMAP_SIZE, event_path, page_response, public_base, sitemap_document
from .waveforms import envelope, load_records, LiveWaveforms

ROOT = Path(__file__).resolve().parents[1]


def passed_candidates(result):
    """Candidate IDs that passed the research quality checks for this revision."""
    selection = result.get("selection") or {}
    passed = set(selection.get("candidates") or [])
    if selection.get("representative"):
        passed.add(selection["representative"])
    # First motions re-traced at the passed centroid depth belong with that solution.
    if passed:
        passed.add("first_motion_centroid")
    return passed


def mechanism_agreement(candidates, passed):
    """Largest Kagan angle among passed solutions, or among all when fewer than two passed."""
    from itertools import combinations

    import numpy as np

    from ..processing.cmt import kagan

    current = [
        row
        for row in candidates
        if row.get("tensor_ned_nm") is not None and not row.get("previous_location")
    ]
    chosen = [row for row in current if row["id"] in passed]
    scope = "passed" if len(chosen) >= 2 else "all"
    rows = chosen if scope == "passed" else current
    angles = []
    for left, right in combinations(rows, 2):
        try:
            angle = kagan(np.asarray(left["tensor_ned_nm"]), np.asarray(right["tensor_ned_nm"]))
        except (TypeError, ValueError, np.linalg.LinAlgError):
            angle = None
        if angle is not None:
            angles.append(angle)
    if not angles:
        return None
    return dict(scope=scope, count=len(rows), max_kagan_deg=max(angles))


def create_app(settings=None, online=True):
    settings = settings or Settings.read()
    from ..processing import exclusions

    exclusions.configure(settings.excluded_channels)
    store = EventStore(settings.database)
    store.migrate(settings.root / "runtime/reports/ledger.json")
    from .monitor import Monitor

    monitor = Monitor(settings, store)
    from .enrichment import BatsEnrichment

    enrichment = BatsEnrichment(settings, store, monitor.publication_lock)

    from .gdms import GdmsEnrichment

    gdms = GdmsEnrichment(settings, store, monitor.publication_lock)
    from .focal import FocalEnrichment

    focal = FocalEnrichment(settings, store, monitor.publication_lock)
    from .regional_cmt import RegionalCMT, public_cmt
    from .mechanism_plot import public_mechanism

    regional = RegionalCMT(settings, store, monitor.publication_lock)
    from .adaptive_cmt import AdaptiveCMT

    adaptive = AdaptiveCMT(settings, store, monitor.publication_lock)

    def research_state(event):
        """(research pick or None, settled) for the latest public revision of ``event``.

        Shared by the website map and Discord, so both draw the same second ball.
        """
        from .review import review_of

        latest = store.get(event["id"])
        if (
            latest is None
            or latest["revision"] != event["revision"]
            or latest.get("summary_only")
            or review_of(latest)["error"]
        ):
            return None, True
        return adaptive.representative(latest)

    def approved_cmt(event):
        result = event.get("regional_cmt")
        if result and not event.get("summary_only") and regional.publishable(result):
            return public_cmt(result)
        return None

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        storage_stop = threading.Event()
        if online:
            from .notifications import outbox
            from .storage import retention_loop

            tasks = [
                asyncio.create_task(monitor.run()),
                asyncio.create_task(
                    retention_loop(settings, store, monitor.publication_lock, storage_stop)
                ),
                asyncio.create_task(
                    outbox(
                        store,
                        settings.webhook,
                        settings.root / "runtime/reports",
                        approved_cmt=approved_cmt,
                        research=research_state if settings.adaptive_cmt_enabled else None,
                        public_url=settings.public_url,
                    )
                ),
            ]
            if settings.gdms_enabled:
                tasks.append(asyncio.create_task(gdms.run()))
            if settings.bats_enabled:
                tasks.append(asyncio.create_task(enrichment.run()))
            if settings.gdms_mechanism_publish:
                tasks.append(asyncio.create_task(focal.run()))
            if settings.regional_cmt_enabled:
                tasks.append(asyncio.create_task(regional.run()))
            if settings.adaptive_cmt_enabled:
                tasks.extend(asyncio.create_task(adaptive.run(kind)) for kind in adaptive.kinds())
        try:
            yield
        finally:
            enrichment.stopping = True
            gdms.stopping = True
            focal.stopping = True
            regional.stopping = True
            adaptive.stopping = True
            storage_stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Tighnari-SeisLOC", version="0.2.0", lifespan=lifespan)
    app.add_middleware(NegotiatedGZipMiddleware, minimum_size=1000, compresslevel=5)
    app.state.store = store
    app.state.monitor = monitor
    app.state.enrichment = enrichment
    app.state.gdms = gdms
    app.state.focal = focal
    app.state.regional_cmt = regional
    app.state.adaptive_cmt = adaptive
    app.state.shutting_down = False
    from .admin import install_admin

    install_admin(app, settings, store, monitor.publication_lock)

    def state():
        latest = store.latest_summary("live")
        active = latest if latest and -10 < time.time() - latest["origin"] < 300 else None
        records, picks = monitor.snapshot()
        stations = {r.id: r.station() for r in records}
        source_time = max((r.status.get("data_time", 0) or 0 for r in monitor.receivers), default=0)
        return dict(
            project="Tighnari-SeisLOC",
            mode="iris" if online else "offline",
            now=time.time(),
            event=active,
            latest_event=latest,
            stations=list(stations.values()),
            sources=(
                {
                    **{s.provider: dict(s.status) for s in monitor.receivers},
                    **({"bats": dict(enrichment.status)} if settings.bats_enabled else {}),
                    "gdms": dict(gdms.status),
                    "focal": dict(focal.status),
                    **(
                        {"regional_cmt": dict(regional.status)}
                        if settings.regional_cmt_enabled
                        else {}
                    ),
                }
                if online
                else {}
            ),
            analysis_at=monitor.analysis_at,
            data_time=monitor.data_time,
            data_delay_s=max(0, time.time() - monitor.data_time) if monitor.data_time else None,
            source_delay_s=max(0, time.time() - source_time) if source_time else None,
            processing_lag_s=max(0, source_time - monitor.data_time)
            if source_time and monitor.data_time
            else None,
            processing=monitor.processing,
            error=monitor.error,
            phase_candidates=len(picks),
            picking=monitor.live_picker.status(settings.picker),
            status="定位中"
            if active
            else "接收與分析中"
            if records
            else "等待測站資料"
            if online
            else "離線檢視",
            playback=dict(playing=True, finished=False, error=monitor.error),
            notification_enabled=bool(settings.webhook),
        )

    live_waveforms = LiveWaveforms(monitor.snapshot)
    app.state.live_waveforms = live_waveforms

    @app.get("/api/health")
    def health():
        return {"ready": True, "project": "Tighnari-SeisLOC", "online": online}

    @app.get("/api/state")
    def api_state():
        return state()

    @app.get("/api/export")
    def export():
        return dict(**state(), earthquakes=store.listing("all", 100)["items"])

    @app.get("/report_list.json")
    def report_list():
        # Live records only: replay runs must never appear as a new public earthquake.
        # Missing ML is intentionally retained, including pending candidates.
        return JSONResponse(
            store.listing("live", 5, require_ml=False)["items"],
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/earthquakes")
    def earthquakes(
        mode: str = Query("all", pattern="^(all|live|historical)$"),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        require_ml: bool = False,
    ):
        return store.listing(mode, limit, offset, require_ml=require_ml)

    @app.get("/api/records")
    def records(
        start: date | None = None,
        end: date | None = None,
        mode: str = Query("live", pattern="^(all|live|historical)$"),
        status: str = Query("located", pattern="^(all|located)$"),
        exclude_no_magnitude: bool = False,
    ):
        from .records import catalogue

        end = end or datetime.now(ZoneInfo("Asia/Taipei")).date()
        start = start or end - timedelta(days=6)
        if start > end:
            raise HTTPException(422, "開始日期不得晚於結束日期")
        if (end - start).days > 3660:
            raise HTTPException(422, "單次查詢最多十年，請縮小日期範圍")
        try:
            return catalogue(store, start, end, mode, status, exclude_no_magnitude)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(422, "日期超出可查詢範圍")

    def event_for(eid):
        event = store.get(eid)
        if event is None:
            raise HTTPException(404, "找不到事件")
        return event

    def public_event(event):
        # Filesystem paths and legacy private settings are not part of the public API.
        event = dict(event)
        from .records import depth_label

        event["depth_display"] = depth_label(event)
        if event.get("summary_only"):
            return event
        result = approved_cmt(event)
        event.pop("regional_cmt", None)
        if result is not None:
            event["regional_cmt"] = result
            if result.get("tensor_ned_nm") is not None:
                from ..processing.focal import polarity_agreement

                check = polarity_agreement(
                    result["tensor_ned_nm"], event.get("focal_mechanism"), event
                )
                if check is not None:
                    result["first_motion_check"] = check
        event["preferred_mechanism"], conflict = public_mechanism(event, result)
        if conflict is not None:
            event["mechanism_conflict"] = conflict
        focal = event.get("focal_mechanism") or {}
        if comparison := focal.get("regional_comparison"):
            legacy = {k: v for k, v in comparison.items() if k != "joint_sh"}
            if "joint" in legacy:
                legacy["joint"] = {
                    k: v for k, v in legacy["joint"].items() if k != "cross_validation"
                }
            event["focal_mechanism"] = dict(focal, regional_comparison=legacy)
        event.setdefault("map_radius_km", settings.map_radius_km)
        from .reporting import event_circles, circle_points

        event["p_arrival_circles"] = [
            dict(circle, points=circle_points(circle))
            for circle in event_circles(event, settings.root / "runtime/iasp91.npz")
        ]
        return {
            k: v
            for k, v in event.items()
            if k
            not in (
                "image_file",
                "closeup_image_file",
                "waveform_file",
                "waveform_image_file",
                "legacy_image",
            )
        }

    @app.get("/api/earthquakes/{eid}")
    def earthquake(eid: str, revision: int | None = Query(None, ge=1)):
        event = store.get(eid, revision)
        if event is None:
            current = store.get(eid)
            if current and current.get("summary_only"):
                raise HTTPException(410, "此地震已超過詳細資料保存期限，僅保留摘要")
            raise HTTPException(404, "找不到事件或修訂")
        return public_event(event)

    @app.get("/api/earthquakes/{eid}/revisions")
    def revisions(eid: str):
        event_for(eid)
        return store.revisions(eid)

    @app.get("/api/earthquakes/{eid}/image")
    def image(
        eid: str,
        request: Request,
        revision: int | None = Query(None, ge=1),
        view: str = Query("overview", pattern="^(overview|closeup)$"),
    ):
        event = store.get(eid, revision)
        if event is None:
            current = store.get(eid)
            if current and current.get("summary_only"):
                raise HTTPException(410, "此地震已超過詳細資料保存期限，僅保留摘要")
            raise HTTPException(404, "找不到事件或修訂")
        if event.get("summary_only"):
            raise HTTPException(410, "此地震已超過詳細資料保存期限，僅保留摘要")
        from .reporting import report_image

        event.setdefault("map_radius_km", settings.map_radius_km)
        target = report_image(
            event,
            settings.root / "runtime/reports",
            travel_cache=settings.root / "runtime/iasp91.npz",
            view=view,
            approved_cmt=approved_cmt(event),
            research=research_pick(event),
        )
        return cached_file(request, target, media_type="image/png", filename=target.name)

    def research_pick(event):
        """Research representative shown beside the official ball, latest revision only."""
        return research_state(event)[0]

    @app.get("/api/earthquakes/{eid}/mechanism-image")
    def mechanism_image(eid: str, request: Request, revision: int | None = Query(None, ge=1)):
        event = store.get(eid, revision)
        if not event:
            raise HTTPException(404, "找不到事件或修訂")
        if event.get("summary_only"):
            raise HTTPException(410, "詳細資料已封存")
        from .mechanism_plot import mechanism_image as render_mechanism

        directory = settings.root / "runtime/reports"
        directory.mkdir(parents=True, exist_ok=True)
        # Hash the ID rather than using externally supplied path components.
        import hashlib

        name = hashlib.sha256(event["id"].encode()).hexdigest()
        path = directory / f"{name}-r{event['revision']}-v3-mechanism.png"
        if not path.exists():
            render_mechanism(event, path)
        return cached_file(request, path, media_type="image/png")

    @app.get("/api/earthquakes/{eid}/regional-cmt-image")
    def regional_cmt_image(
        eid: str, revision: int | None = Query(None, ge=1), candidate: str | None = None
    ):
        from io import BytesIO

        from .mechanism_plot import beachball

        event = store.get(eid, revision) or {}
        result = approved_cmt(event) or {}
        if result.get("status") not in (
            "experimental",
            "ambiguous",
        ):
            raise HTTPException(404, "此修訂沒有可展示的臺灣區域 CMT")
        if candidate is not None:
            result = next(
                (row for row in result.get("solutions", []) if row["id"] == candidate), {}
            )
        if "tensor_ned_nm" not in result:
            raise HTTPException(404, "此修訂沒有可展示的候選解")
        output = BytesIO()
        beachball(result["tensor_ned_nm"], 220).save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png")

    def research_event(eid):
        """Latest public revision whose automatic mechanism research may be shown."""
        from .review import review_of

        event = store.get(eid)
        if event is None or review_of(event)["error"]:
            raise HTTPException(404, "找不到事件")
        if event.get("summary_only"):
            raise HTTPException(410, "詳細資料已封存")
        return event

    def research_candidate(event, candidate_id, field):
        result = adaptive.diagnostics(event) or {}
        candidate = next(
            (
                row
                for row in result.get("candidates", [])
                if row["id"] == candidate_id and row.get(field)
            ),
            None,
        )
        if candidate is None:
            raise HTTPException(404, "找不到機制候選")
        return candidate

    @app.get("/api/earthquakes/{eid}/mechanisms")
    def public_mechanisms(eid: str):
        """Automatic mechanism research of the latest revision; not the reviewed product.

        Like AutoBATS and RMT, the public page draws only solutions that passed: failed
        candidates keep their diagnostics but lose the tensor, so no beach ball is shown.
        Hoshi reads the unfiltered diagnostics.
        """
        event = research_event(eid)
        result = adaptive.diagnostics(event)
        if result is None:
            raise HTTPException(404, "此事件沒有自動機制試算")
        passed = passed_candidates(result)
        candidates = []
        for row in result.get("candidates", []):
            row = dict(row, passed=row["id"] in passed)
            if not row["passed"]:
                for key in ("tensor_ned_nm", "nodal_planes"):
                    row.pop(key, None)
            candidates.append(row)
        return dict(
            result,
            candidates=candidates,
            agreement=mechanism_agreement(result.get("candidates", []), passed),
            scope="public_research",
            revision=event["revision"],
        )

    @app.get("/api/earthquakes/{eid}/mechanisms/{candidate_id}.png")
    def public_mechanism_ball(eid: str, candidate_id: str):
        from io import BytesIO

        from .mechanism_plot import beachball

        event = research_event(eid)
        if candidate_id not in passed_candidates(adaptive.diagnostics(event) or {}):
            raise HTTPException(404, "此候選未通過品質檢查，不提供海灘球")
        candidate = research_candidate(event, candidate_id, "tensor_ned_nm")
        output = BytesIO()
        beachball(candidate["tensor_ned_nm"], 120).save(output, format="PNG")
        return Response(
            output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/earthquakes/{eid}/mechanisms/{candidate_id}/fits.png")
    def public_mechanism_fits(eid: str, candidate_id: str):
        event = research_event(eid)
        research_candidate(event, candidate_id, "fit_image")
        path = adaptive.fit_path(event, candidate_id)
        if path is None:
            raise HTTPException(404, "找不到波形擬合圖")
        return Response(
            path.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/earthquakes/{eid}/waveform-image")
    def waveform_image(eid: str, request: Request, revision: int | None = Query(None, ge=1)):
        event = store.get(eid, revision)
        if event and event.get("summary_only"):
            raise HTTPException(410, "此地震已超過詳細資料保存期限，僅保留摘要")
        if event is None or not event.get("waveform_image_file"):
            raise HTTPException(404, "此修訂沒有波形封存圖片")
        path = Path(event["waveform_image_file"]).resolve()
        if (
            not path.is_relative_to((settings.root / "runtime/reports").resolve())
            or not path.is_file()
        ):
            raise HTTPException(404, "找不到波形封存圖片")
        return cached_file(request, path, media_type="image/png")

    @app.get("/api/earthquakes/{eid}/download")
    def download(eid: str, revision: int | None = Query(None, ge=1)):
        event = store.get(eid, revision)
        if event is None:
            current = store.get(eid)
            if current and current.get("summary_only"):
                raise HTTPException(410, "此地震已超過詳細資料保存期限，僅保留摘要")
            raise HTTPException(404, "找不到事件或修訂")
        data = json.dumps(public_event(event), ensure_ascii=False, indent=2, allow_nan=False)
        from fastapi.responses import Response

        return Response(
            data,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{eid}.json"'},
        )

    @app.get("/reports/{name}")
    def legacy_report(name: str):
        from .reporting import CMT_REPORT_MARKER

        folder = (settings.root / "runtime/reports").resolve()
        target = (folder / name).resolve()
        # CMT variants are served by the revision endpoint, which rechecks publication.
        if CMT_REPORT_MARKER in target.name:
            raise HTTPException(404, "請由事件頁取得報告圖片")
        event = store.get(name.split("-r", 1)[0])
        if event is None:
            raise HTTPException(404, "找不到報告圖片")
        if event and event.get("summary_only"):
            raise HTTPException(410, "此地震僅保留摘要")
        with store.connect() as db:
            if db.execute(
                "SELECT 1 FROM retention_deletions WHERE path=?", (str(target),)
            ).fetchone():
                raise HTTPException(410, "此地震僅保留摘要")
        if not target.is_relative_to(folder) or target.suffix != ".png" or not target.is_file():
            raise HTTPException(404, "找不到報告圖片")
        return FileResponse(target, media_type="image/png")

    @lru_cache(maxsize=8)
    def archived(path, mtime):
        return load_records(path)

    def wave_data(
        ids=None, seconds=60, eid=None, end=None, bins=900, revision=None, include_candidates=False
    ):
        if eid:
            event = store.get(eid, revision)
            if event is None:
                current = store.get(eid)
                if current and current.get("summary_only"):
                    raise HTTPException(410, "此地震已超過詳細資料保存期限，僅保留摘要")
                raise HTTPException(404, "找不到事件或修訂")
            if event.get("summary_only"):
                return dict(start=0, end=0, rows=[], unit="µm/s", storage="summary")
            path = event.get("waveform_file")
            if not path or not Path(path).exists():
                archive = event.get("waveform_archive")
                if archive and event.get("waveform_image_file"):
                    return dict(
                        start=archive["start"],
                        end=archive["end"],
                        rows=[],
                        unit="µm/s",
                        storage="image",
                        stations=archive["stations"],
                        image_url=f"/api/earthquakes/{eid}/waveform-image?revision={event['revision']}",
                    )
                return dict(start=0, end=0, rows=[], unit="µm/s")
            records = archived(path, Path(path).stat().st_mtime)
            adopted = [
                dict(
                    p,
                    id=p.get("source_id", p["id"]),
                    time=p.get("observed", p.get("time")),
                    adopted=True,
                )
                for p in event.get("residuals", [])
                if p.get("used")
            ]
            picks = adopted
            if include_candidates:
                keys = {(p["id"], p["phase"], round(p["time"], 2)) for p in adopted}
                picks += [
                    dict(p, adopted=False)
                    for p in event.get("picks", [])
                    if (p["id"], p["phase"], round(p["time"], 2)) not in keys
                ]

            end = (
                end if end is not None else min(max(r.end for r in records), event["origin"] + 100)
            )
        else:
            records, picks = monitor.snapshot()
        return envelope(records, ids, seconds, end, bins, picks)

    @app.get("/api/waveforms")
    async def waveforms(
        stations: str = "",
        seconds: int = Query(60, ge=10, le=600),
        event_id: str | None = None,
        end: float | None = None,
        bins: int = Query(900, ge=30, le=1200),
        revision: int | None = Query(None, ge=1),
        include_candidates: bool = False,
    ):
        ids = stations.split(",")[:16] if stations else None
        if not event_id and end is None:
            frame, _ = await live_waveforms.get(ids, seconds, bins, default_limit=None)
            return frame
        return await asyncio.to_thread(
            wave_data, ids, seconds, event_id, end, bins, revision, include_candidates
        )

    @app.websocket("/ws/waveforms")
    async def waveform_socket(socket: WebSocket):
        await socket.accept()
        request = {"stations": None, "seconds": 60}
        last_end = None
        last_versions = {}
        last_rows = set()
        try:
            while not app.state.shutting_down:
                try:
                    message = await asyncio.wait_for(socket.receive_json(), timeout=1)
                    if not isinstance(message, dict):
                        continue
                    station_ids = message.get("stations", [])
                    if not isinstance(station_ids, list) or not all(
                        isinstance(s, str) for s in station_ids
                    ):
                        continue
                    try:
                        request = dict(
                            stations=station_ids[:16],
                            seconds=min(600, max(10, int(message.get("seconds", 60)))),
                            bins=min(1200, max(30, int(message.get("bins", 900)))),
                        )
                    except (ValueError, TypeError, OverflowError):
                        await socket.send_json(
                            {"type": "error", "detail": "時間窗與 bins 必須為整數"}
                        )
                        continue
                    last_end = None
                except asyncio.TimeoutError:
                    pass
                frame, versions = await live_waveforms.get(
                    request["stations"], request["seconds"], request.get("bins", 900)
                )
                if last_end is not None and versions == last_versions:
                    continue
                if last_end is None or frame["end"] != last_end or versions != last_versions:
                    visible_rows = {row["id"] for row in frame["rows"]}
                    if last_end is not None:
                        # Replace changed station rows within the requested window.
                        # A global timestamp cutoff would discard delayed packets.
                        frame["rows"] = [
                            row
                            for row in frame["rows"]
                            if versions.get(row["id"]) != last_versions.get(row["id"])
                            or row["id"] not in last_rows
                        ]
                    frame["replace_rows"] = True
                    frame["removed"] = sorted(last_rows - visible_rows)
                    frame["type"] = "snapshot" if last_end is None else "delta"
                    await asyncio.wait_for(socket.send_json(frame), timeout=10)
                    last_end = frame["end"]
                    last_versions, last_rows = versions, visible_rows
            await socket.close(code=1001)
        except (WebSocketDisconnect, RuntimeError, asyncio.TimeoutError):
            return

    snapshot_lock = asyncio.Lock()
    cached_snapshot = None
    snapshot_at = 0

    async def stream_snapshot():
        nonlocal cached_snapshot, snapshot_at
        # All viewers share one state/database read per second.
        async with snapshot_lock:
            if cached_snapshot is None or time.monotonic() - snapshot_at >= 1:
                snapshot = await asyncio.to_thread(state)
                snapshot["earthquakes"] = await asyncio.to_thread(store.summary_items, "all", 20)
                cached_snapshot = snapshot
                snapshot_at = time.monotonic()
            return dict(cached_snapshot)

    revision_cache = SharedTTLCache()
    stream_cache = SharedTTLCache(maxsize=2)

    async def stream_json(compact):
        snapshot = await stream_snapshot()
        if compact:
            snapshot["earthquakes"] = [
                {
                    "id": e["id"],
                    "revision": e["revision"],
                    "summary_only": bool(e.get("summary_only")),
                }
                for e in snapshot["earthquakes"]
            ]
        return json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    async def sse(request, compact=False, event_id=None):
        previous = None
        heartbeat = time.monotonic()
        while not app.state.shutting_down and not await request.is_disconnected():
            if event_id:
                # Detail pages only need this event's revision, including older events.
                revision = await revision_cache.get(
                    event_id, lambda: asyncio.to_thread(store.revision_state, event_id)
                )
                snapshot = {"id": event_id, **revision}
                raw = json.dumps(snapshot, separators=(",", ":"))
            else:
                raw = await stream_cache.get(compact, lambda: stream_json(compact))
            if raw != previous:
                yield "data: " + raw + "\n\n"
                previous = raw
                heartbeat = time.monotonic()
            elif time.monotonic() - heartbeat >= 15:
                yield ": keepalive\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(1)

    @app.get("/api/events")
    @app.get("/api/stream")
    async def stream(request: Request, compact: bool = False, event_id: str | None = None):
        if event_id:
            await asyncio.to_thread(event_for, event_id)
        return StreamingResponse(
            sse(request, compact, event_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/waveform")
    async def ingest(request: Request):
        from ..models import WavePacket

        token = load_settings(settings.root).get("INGEST_TOKEN")
        if not token or not online:
            raise HTTPException(403, "未啟用波形輸入")
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "驗證失敗")
        body = await request.body()
        if len(body) > 3_000_000:
            raise HTTPException(413, "封包過大")
        try:
            packet = WavePacket.model_validate_json(body)
        except ValueError:
            raise HTTPException(422, "波形格式無效")
        if abs(time.time() - packet.start - len(packet.samples) / packet.rate) > 15:
            raise HTTPException(422, "波形逾時")
        if len(monitor.own_buffers) >= 1000 and packet.station.id not in monitor.own_buffers:
            raise HTTPException(429, "測站數量已達上限")
        return {"accepted": monitor.ingest(packet)}

    @app.post("/api/playback")
    def playback():
        raise HTTPException(409, "顯示暫停由瀏覽器控制，不停止接收")

    @app.get("/taiwan.json")
    def taiwan(request: Request):
        return cached_file(request, ROOT / "legacy/assets/taiwan.json")

    @app.get("/geography/{name}")
    def geography(name: str, request: Request):
        if name not in (
            "regional_land.geojson",
            "taiwan_towns.geojson",
            "taiwan_towns_display.geojson",
        ):
            raise HTTPException(404)
        path = ROOT / "assets" / (name + ".gz")
        # Negotiate the precompressed representation without decompress/recompress work.
        if accepts_gzip(request.headers.get("accept-encoding", "")):
            return cached_file(
                request,
                path,
                media_type="application/geo+json",
                headers={
                    "Content-Encoding": "gzip",
                    "Vary": "Accept-Encoding",
                    "Cache-Control": "public, max-age=3600",
                },
            )
        # A distinct ETag prevents mixing identity and gzip cache representations.
        response = FileResponse(path, stat_result=path.stat())
        etag = response.headers["etag"].strip('"')
        headers = {
            "Cache-Control": "public, max-age=3600",
            "Vary": "Accept-Encoding",
            "ETag": '"' + etag + '-identity"',
        }
        if StaticFiles().is_not_modified(Headers(headers), request.headers):
            return NotModifiedResponse(headers)
        return Response(
            gzip.decompress(path.read_bytes()), media_type="application/geo+json", headers=headers
        )

    def site_base(request):
        return public_base(request, settings.public_url)

    @app.get("/robots.txt", include_in_schema=False)
    def robots(request: Request):
        return Response(
            "User-agent: *\nAllow: /\nDisallow: /docs\n"
            "Disallow: /redoc\nDisallow: /openapi.json\nDisallow: /hoshi\n"
            f"\nSitemap: {site_base(request)}/sitemap.xml\n",
            media_type="text/plain",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap(request: Request):
        pages = (store.sitemap_count() + SITEMAP_SIZE - 1) // SITEMAP_SIZE
        paths = ["/sitemap-pages.xml"] + [
            f"/sitemap-events-{page}.xml" for page in range(1, pages + 1)
        ]
        return sitemap_document(site_base(request), paths, index=True)

    @app.get("/sitemap-pages.xml", include_in_schema=False)
    def sitemap_pages(request: Request):
        return sitemap_document(site_base(request), ["/", "/records", "/about"])

    @app.get("/sitemap-events-{page}.xml", include_in_schema=False)
    def sitemap_events(request: Request, page: int):
        if page < 1:
            raise HTTPException(404)
        ids = store.sitemap_ids(SITEMAP_SIZE, (page - 1) * SITEMAP_SIZE)
        if not ids:
            raise HTTPException(404)
        return sitemap_document(site_base(request), [event_path(eid) for eid in ids])

    @app.get("/")
    def index(request: Request):
        return page_response(
            ROOT / "web/index.html",
            site_base(request),
            "/",
            "Tighnari SeisLOC 地震觀測站：查看即時地震定位、震央、規模與測站波形。自動研究估算，尚未經人工覆核。",
        )

    @app.get("/records")
    def records_page(request: Request):
        return page_response(
            ROOT / "web/records.html",
            site_base(request),
            "/records",
            "查詢 Tighnari SeisLOC 地震記錄，依日期檢視震央分布、規模、深度與定位品質。",
        )

    @app.get("/about")
    def about_page(request: Request):
        return page_response(
            ROOT / "web/about.html",
            site_base(request),
            "/about",
            "關於 Tighnari SeisLOC 地震定位系統。",
        )

    @app.get("/events/{eid}")
    def detail(eid: str, request: Request):
        event = event_for(eid)
        place = event.get("place") or "推估震央"
        origin = datetime.fromtimestamp(event["origin"], ZoneInfo("Asia/Taipei"))
        description = (
            f"{origin:%Y-%m-%d %H:%M:%S} UTC+8，{place}地震記錄。"
            "查看震源參數、定位品質與觀測資料。"
            + (
                "已人工確認。"
                if event.get("review", {}).get("status") == "confirmed"
                else "自動研究估算，尚未經人工覆核。"
            )
        )
        return page_response(
            ROOT / "web/detail.html",
            site_base(request),
            event_path(eid),
            description,
            title=f"{origin:%Y-%m-%d %H:%M} {place} · Tighnari SeisLOC",
            summary=description,
        )

    app.mount("/static", CachedStaticFiles(directory=ROOT / "web"), name="static")
    return app
