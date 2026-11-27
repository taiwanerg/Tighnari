"""Reproducible public-query benchmark; uses synthetic data in a temporary DB.

Run: PYTHONPATH=src .venv/bin/python scripts/benchmark_summary.py
"""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import platform
import statistics
import tempfile
import time

import httpx
from starlette.requests import Request

from tighnari_seisloc.config import Settings
from tighnari_seisloc.services.app import create_app
from tighnari_seisloc.services.review import visible_sql
from tighnari_seisloc.services.store import EventStore, ML_SQL


def measure(function, samples):
    values = []
    for _ in range(samples):
        start = time.perf_counter()
        function()
        values.append((time.perf_counter() - start) * 1000)
    return stats(values)


def stats(values):
    return dict(
        median_ms=round(statistics.median(values), 3),
        p95_ms=round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--payload-kib", type=int, default=128)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    if min(args.events, args.payload_kib, args.samples) < 1:
        parser.error("events, payload-kib and samples must be positive")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "events.db"
        store = EventStore(path)
        with store.connect() as db:
            for i in range(args.events):
                event = dict(
                    id=str(i),
                    origin=1000 + i,
                    mode="live" if i % 5 else "historical",
                    status="located",
                    revision=1,
                    magnitude_type="ML",
                    magnitude=3,
                    magnitudes=[dict(type="ML", value=i % 7 - 1)],
                    review=dict(deleted=i % 13 == 0, error=i % 17 == 0),
                    diagnostics="x" * (args.payload_kib * 1024),
                )
                db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                    (
                        str(i),
                        event["origin"],
                        event["mode"],
                        "located",
                        1,
                        json.dumps(event),
                        1000 + i,
                    ),
                )
            db.execute("DELETE FROM metadata WHERE key='event_summaries_version'")
        start = time.perf_counter()
        store = EventStore(path)
        migration_ms = (time.perf_counter() - start) * 1000

        def legacy(mode="all", limit=20, require_ml=False):
            where = visible_sql()
            params = []
            if mode != "all":
                where += " AND mode=?"
                params.append(mode)
            if require_ml:
                where += " AND (" + ML_SQL + ")"
            with store.connect() as db:
                total = db.execute("SELECT count(*) FROM events WHERE " + where, params).fetchone()[
                    0
                ]
                rows = db.execute(
                    "SELECT payload FROM events WHERE " + where + " ORDER BY origin DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
            return dict(
                total=total,
                limit=limit,
                offset=0,
                items=[store.summary(json.loads(row[0])) for row in rows],
            )

        # Compare public output across every mode/filter, not just timings.
        for mode in ("all", "live", "historical"):
            for ml in (False, True):
                assert legacy(mode, require_ml=ml) == store.listing(mode, require_ml=ml)
        report = dict(
            environment=dict(
                platform=platform.platform(),
                python=platform.python_version(),
                events=args.events,
                payload_kib=args.payload_kib,
                samples=args.samples,
                query_workers=1,
                concurrent_workers=8,
            ),
            migration_ms=round(migration_ms, 3),
        )
        report["queries"] = {
            "legacy_list_ml": measure(lambda: legacy("live", require_ml=True), args.samples),
            "summary_list_ml": measure(
                lambda: store.listing("live", require_ml=True), args.samples
            ),
            "legacy_latest": measure(lambda: legacy("live", 1), args.samples),
            "summary_latest": measure(store.latest_summary, args.samples),
            "legacy_stream_reads": measure(lambda: (legacy("live", 1), legacy()), args.samples),
            "summary_stream_reads": measure(
                lambda: (store.latest_summary(), store.summary_items()), args.samples
            ),
        }
        with ThreadPoolExecutor(max_workers=8) as pool:

            def concurrent_query(_):
                start = time.perf_counter()
                store.listing("live", require_ml=True)
                return (time.perf_counter() - start) * 1000

            report["concurrent_list"] = stats(
                list(pool.map(concurrent_query, range(max(40, args.samples))))
            )
        app = create_app(Settings(root, path, None), online=False)

        async def http_timings():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                results = {}
                for endpoint in ("/api/earthquakes?mode=live&require_ml=true", "/api/state"):
                    values = []
                    for _ in range(args.samples + 1):
                        start = time.perf_counter()
                        response = await client.get(endpoint)
                        response.raise_for_status()
                        values.append((time.perf_counter() - start) * 1000)
                    results[endpoint] = stats(values[1:])
                endpoint = next(
                    r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/stream"
                )
                values = []
                for _ in range(args.samples):

                    async def receive():
                        return {"type": "http.request", "body": b"", "more_body": False}

                    request = Request(
                        {"type": "http", "method": "GET", "path": "/api/stream", "headers": []},
                        receive,
                    )
                    start = time.perf_counter()
                    response = await endpoint(request, compact=True)
                    try:
                        raw = await anext(response.body_iterator)
                        assert json.loads(raw.removeprefix("data: "))["latest_event"]
                        values.append((time.perf_counter() - start) * 1000)
                    finally:
                        await response.body_iterator.aclose()
                results["stream_first_data_in_process"] = stats(values)
                results["stream_uncached_first_ms"] = round(values[0], 3)
                return results

        report["endpoints_in_process"] = asyncio.run(http_timings())
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
