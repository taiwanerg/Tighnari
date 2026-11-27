"""Shared SeedLink receivers and nonblocking regional analysis workers."""

import asyncio
import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
import hashlib
from ..acquisition.fdsn import PROVIDERS
from ..acquisition.stations import INVENTORY_BOUNDS, select_stations
from ..processing.event_identity import same_event
from ..logging_utils import RepeatedLog
from ..processing.streaming import LiveCalibration, LivePicker
from ..legacy.location import distance
from .reporting import report_images
from .waveforms import save_records

LOG = logging.getLogger(__name__)


class Receiver:
    def __init__(self, provider, server, limit=60, event=None, max_data_delay_s=1800):
        from obspy import Stream

        self.provider, self.server, self.limit, self.event = provider, server, limit, event
        self.stream = Stream()
        self.inventory = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.client = None
        self.status = dict(state="取得測站響應資料", count=0, updated=None)
        self.thread = None
        self.max_data_delay_s = max_data_delay_s
        self.generation = 0
        self.recent_packets = OrderedDict()
        self.messages = RepeatedLog(LOG)

    def on_data(self, trace):
        from obspy import UTCDateTime, Stream

        now = time.time()
        horizon = 1200 if self.event else 600
        end = float(trace.stats.endtime)
        if end > now + 30 or now - end > self.max_data_delay_s:
            return
        key = (
            trace.id,
            float(trace.stats.starttime),
            len(trace),
            hashlib.blake2b(trace.data.tobytes(), digest_size=8).digest(),
        )
        with self.lock:
            if key in self.recent_packets:
                return
            group = self.stream.select(id=trace.id).copy()
            channel_end = max([end] + [float(t.stats.endtime) for t in group])
            if end < channel_end - horizon:
                return
            group += trace.copy()
            group.merge(method=0, fill_value=None)
            group.trim(UTCDateTime(channel_end - horizon))
            for tr in group:
                tr.stats.processing = []
            self.stream = (
                Stream(
                    [
                        t
                        for t in self.stream
                        if t.id != trace.id
                        and now - float(t.stats.endtime) <= self.max_data_delay_s + horizon
                    ]
                )
                + group
            )
            self.recent_packets[key] = True
            while len(self.recent_packets) > 8192:
                self.recent_packets.popitem(last=False)
            self.generation += 1
            self.status = dict(
                state="接收波形",
                count=len({(t.stats.network, t.stats.station) for t in self.stream}),
                updated=now,
                data_time=max(float(t.stats.endtime) for t in self.stream),
                delay_s=max(0, now - max(float(t.stats.endtime) for t in self.stream)),
            )
        self.messages.recovered("SeedLink %s 已恢復接收", self.provider)

    def run(self):
        from obspy import UTCDateTime
        from obspy.clients.fdsn import Client
        from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

        delay = 2
        while not self.stop.is_set():
            try:
                kwargs = dict(
                    starttime=UTCDateTime(),
                    channel="BH?,HH?,HN?,EH?",
                    level="response",
                    includerestricted=False,
                )
                if self.event:
                    kwargs.update(
                        network="IU,II,GE,G",
                        latitude=self.event["lat"],
                        longitude=self.event["lon"],
                        minradius=30,
                        maxradius=90,
                        channel="BHZ",
                    )
                else:
                    south, north, west, east = INVENTORY_BOUNDS
                    kwargs.update(
                        minlatitude=south, maxlatitude=north, minlongitude=west, maxlongitude=east
                    )
                inventory = Client(PROVIDERS[self.provider], timeout=30).get_stations(**kwargs)
                client = EasySeedLinkClient(self.server, autoconnect=False)
                self.client = client
                client.conn.timeout = 15
                client.conn.set_net_timeout(30)
                client.on_data = self.on_data
                client.on_seedlink_error = lambda: self.status.update(state="串流重連中")
                client.connect()
                info = ET.fromstring(client.get_info("STREAMS"))
                available = {
                    (s.get("network"), s.get("name")): {
                        (c.get("location", ""), c.get("seedname", "")) for c in s.findall("stream")
                    }
                    for s in info.findall("station")
                }
                groups = []
                for net in inventory:
                    for sta in net:
                        channels = available.get((net.code, sta.code), set())
                        selectors = {
                            (c.location_code, c.code[:2])
                            for c in sta
                            if (c.location_code, c.code) in channels and c.sample_rate >= 20
                        }
                        if selectors:
                            groups.append((net.code, sta, selectors))
                if self.event:
                    # Reserve Yonaguni, then take farthest-point sites for geometry.
                    groups.sort(
                        key=lambda g: (
                            0 if (g[0], g[1].code) == ("JP", "YOJ") else 1,
                            float(distance(24, 121, g[1].latitude, g[1].longitude)),
                        )
                    )
                    selected = []
                    if groups:
                        selected.append(groups.pop(0))
                    while groups and len(selected) < self.limit:
                        if not self.event and len(selected) < 20:
                            item = groups[0]
                        else:
                            item = max(
                                groups,
                                key=lambda g: min(
                                    float(
                                        distance(
                                            g[1].latitude,
                                            g[1].longitude,
                                            s[1].latitude,
                                            s[1].longitude,
                                        )
                                    )
                                    for s in selected
                                ),
                            )
                        selected.append(item)
                        groups.remove(item)
                else:
                    rows = [
                        dict(
                            provider=self.provider,
                            network=net,
                            station=sta.code,
                            lat=sta.latitude,
                            lon=sta.longitude,
                        )
                        for net, sta, _ in groups
                    ]
                    ids = {(r["network"], r["station"]) for r in select_stations(rows, self.limit)}
                    selected = [g for g in groups if (g[0], g[1].code) in ids]
                if not selected:
                    raise ValueError("No available SeedLink stations")
                with self.lock:
                    self.inventory = inventory
                for net, sta, selectors in selected:
                    for loc, prefix in selectors:
                        client.select_stream(
                            net, sta.code, loc + prefix + ("Z.D" if self.event else "?.D")
                        )
                self.status.update(state=f"已選取 {len(selected)} 站，等待資料")
                if self.stop.is_set():
                    break
                client.run()
            except Exception as exc:
                self.status.update(state="來源失敗：" + type(exc).__name__ + "；重試中")
                self.messages.warning(
                    type(exc).__name__,
                    "SeedLink %s: %s: %s",
                    self.provider,
                    type(exc).__name__,
                    str(exc)[:180],
                )
            finally:
                if self.client:
                    self.client.close()
                self.client = None
            self.stop.wait(delay)
            delay = min(60, delay * 2)

    def start(self):
        self.thread = threading.Thread(
            target=self.run, daemon=True, name="seedlink-" + self.provider
        )
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return self.stream.copy(), self.inventory

    def terminate(self):
        self.stop.set()
        if self.client:
            self.client.conn.terminate()


class Monitor:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        # GEOFON's regional TQ metadata has no matching SeedLink streams
        # (verified 2026-11-22). Keep its FDSN archive path separate from live input.
        self.receivers = [
            Receiver(
                "earthscope",
                "rtserve.earthscope.org:18000",
                settings.max_stations,
                max_data_delay_s=settings.max_data_delay_s,
            ),
        ]
        self.own_buffers = {}
        self.records = []
        self.picks = []
        self.error = None
        self.analysis_at = None
        self.model = None
        self.stopping = False
        self.far_jobs = {}
        self.lock = threading.Lock()
        self.publication_lock = threading.Lock()
        self.calibration = LiveCalibration()
        self.live_picker = LivePicker()
        self.messages = RepeatedLog(LOG)
        self.data_time = None
        self.processing = False
        self.own_generation = 0
        self.last_generation = None
        self.far_completed = set()
        self.published = {}

    def ingest(self, packet):
        """Accumulate calibrated acceleration before integration; reject overlaps."""
        import numpy as np
        from scipy.integrate import cumulative_trapezoid
        from scipy.signal import butter, sosfilt
        from ..models import Record

        sid = packet.station.id
        with self.lock:
            old = self.own_buffers.get(sid)
            if old and packet.start < old["end"] - 0.5 / packet.rate:
                return False
            continuous = (
                old
                and old["rate"] == packet.rate
                and abs(packet.start - old["end"]) < 1.5 / packet.rate
            )
            samples = (
                np.concatenate([old["a"], np.array(packet.samples)])
                if continuous
                else np.array(packet.samples)
            )
            start = old["start"] if continuous else packet.start
            trim = max(0, len(samples) - int(600 * packet.rate))
            samples = samples[trim:]
            start += trim / packet.rate
            acc = samples / 100
            acc = acc - np.median(acc[: min(len(acc), int(2 * packet.rate))], axis=0)
            vel = cumulative_trapezoid(acc, dx=1 / packet.rate, axis=0, initial=0)
            vel = sosfilt(
                butter(2, 0.01, fs=packet.rate, btype="highpass", output="sos"), vel, axis=0
            )
            st = packet.station
            record = Record(
                sid,
                st.lat,
                st.lon,
                start,
                packet.rate,
                vel,
                st.elevation_m,
                "own-calibrated-acceleration",
            )
            self.own_buffers[sid] = dict(
                a=samples,
                start=start,
                end=packet.start + len(packet.samples) / packet.rate,
                rate=packet.rate,
                record=record,
            )
            self.records = [r for r in self.records if r.id != sid] + [record]
            self.own_generation += 1
            record.metadata["wave_version"] = f"own-{self.own_generation}"
        return True

    def snapshot(self):
        with self.lock:
            return list(self.records), list(self.picks)

    def analyze(self):
        from ..processing.travel import Iasp91
        from ..processing.pipeline import analyze

        generation = (tuple(s.generation for s in self.receivers), self.own_generation)
        if generation == self.last_generation:
            return
        sources = [(s.provider, *s.snapshot()) for s in self.receivers]
        records, errors = self.calibration.update(sources)
        with self.lock:
            expired = [sid for sid, b in self.own_buffers.items() if time.time() - b["end"] > 600]
            for sid in expired:
                del self.own_buffers[sid]
            records.extend(b["record"] for b in self.own_buffers.values())
        if not records:
            self.last_generation = generation
            return
        # Waveform clients receive new calibrated samples before location work.
        with self.lock:
            self.records = records
            self.data_time = max(r.end for r in records)
        if self.model is None:
            self.model = Iasp91(self.settings.root / "runtime/iasp91.npz")
        picked = self.live_picker.update(records, self.settings.picker)
        report = analyze(records, self.model, self.settings.picker, picked=picked)
        with self.lock:
            self.picks = report["picks"]
        self.last_generation = generation
        self.analysis_at = time.time()
        self.error = None if not errors else f"{len(errors)} 個校正片段拒收"
        for event in report["events"]:
            event["map_radius_km"] = self.settings.map_radius_km
            if self.stopping:
                return
            if self.data_time - event["origin"] > 600:
                continue
            with self.publication_lock:
                saved = self.publish(event, report, records)
            if saved is None:
                continue
            if (
                self.settings.live_mwp
                and event["status"] == "located"
                and saved["id"] not in self.far_jobs
                and saved["id"] not in self.far_completed
            ):
                receiver = Receiver(
                    "earthscope",
                    "rtserve.earthscope.org:18000",
                    min(60, max(20, self.settings.max_stations)),
                    event=saved,
                    max_data_delay_s=self.settings.max_data_delay_s,
                )
                with self.lock:
                    self.far_jobs[saved["id"]] = (receiver, saved, time.time())
                receiver.start()

    def publish(self, event, report, records):
        """Save a scientifically changed solution; repeated packets do not create revisions."""
        signature = tuple(
            round(event[k], n) if event.get(k) is not None else None
            for k, n in (("origin", 1), ("lat", 4), ("lon", 4), ("depth_km", 1), ("magnitude", 2))
        ) + (
            event["status"],
            event.get("quality_grade"),
            tuple(event.get("used", [])),
            event.get("phase_count"),
        )
        matched = next(
            (eid for eid, (prior, _) in self.published.items() if same_event(prior, event)),
            None,
        )
        if matched and self.published[matched][1] == signature:
            return None
        previous = self.published.get(matched, ({}, None))[0]
        important_update = event["status"] == "located" and (
            previous.get("status") != "located"
            or (previous.get("magnitude") is None and event.get("magnitude") is not None)
        )
        if matched:
            prior = self.store.get(matched)
            if prior:
                if prior.get("status") == "located" and event["status"] != "located":
                    return None
                event["magnitudes"] += [
                    m for m in prior.get("magnitudes", []) if m["type"] == "Mwp"
                ]
                if prior.get("focal_mechanism"):
                    event["focal_mechanism"] = dict(
                        prior["focal_mechanism"],
                        status="stale",
                        reasons=["定位或波形已更新，等待重新評估機制"],
                    )
        saved = self.store.put(event, "live", matched)
        if saved is None:
            return None
        self.published[saved["id"]] = (dict(event), signature)
        self.published = {
            eid: value
            for eid, value in self.published.items()
            if self.data_time - value[0]["origin"] < 3600
        }
        folder = self.settings.root / "runtime/reports"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{saved['id']}-r{saved['revision']}"
        image = folder / (stem + ".png")
        wavefile = folder / (stem + "-waveforms.npz")
        save_records(records, wavefile)
        saved["waveform_file"] = str(wavefile)
        for source in self.receivers:
            raw, inv = source.snapshot()
            if raw and inv:
                try:
                    target = folder / (stem + "-" + source.provider + ".mseed")
                    raw.split().write(str(target) + ".tmp", format="MSEED")
                    from pathlib import Path

                    Path(str(target) + ".tmp").replace(target)
                    xml = target.with_suffix(".xml")
                    if not xml.exists():
                        inv.write(xml, format="STATIONXML")
                except Exception as exc:
                    self.messages.warning(
                        ("archive", source.provider, type(exc).__name__),
                        "Live archive %s: %s",
                        source.provider,
                        type(exc).__name__,
                    )
        images = report_images(
            saved, folder, travel_cache=self.settings.root / "runtime/iasp91.npz"
        )
        image = images["overview"]
        saved["closeup_image_file"] = str(images["closeup"])
        # Keep artifact updates in the same event revision rather than creating fake scientific revisions.
        saved["image_file"] = str(image)
        with self.store.connect() as db:
            payload = json.dumps(saved, ensure_ascii=False, allow_nan=False)
            db.execute("UPDATE events SET payload=? WHERE id=?", (payload, saved["id"]))
            self.store.update_summary(db, saved)
            db.execute(
                "UPDATE revisions SET payload=? WHERE event_id=? AND revision=?",
                (payload, saved["id"], saved["revision"]),
            )
        (LOG.info if important_update else LOG.debug)(
            "地震定位 %s / ML %s / 深度 %.1f km / %d 站 / 修訂 %d",
            saved.get("place"),
            saved.get("magnitude"),
            saved["depth_km"],
            saved["count"],
            saved["revision"],
        )
        return saved

    def update_far(self):
        import numpy as np
        from obspy.taup import TauPyModel
        from ..processing.magnitude import measure_mwp, aggregate_mwp

        with self.lock:
            jobs = list(self.far_jobs.items())
        if not jobs:
            return
        model = TauPyModel("iasp91")
        for eid, (receiver, initial, started) in jobs:
            if self.stopping:
                return
            if time.time() - started > 1800:
                receiver.terminate()
                with self.lock:
                    self.far_jobs.pop(eid, None)
                    self.far_completed.add(eid)
                continue
            initial = self.store.get(eid)
            if initial is None or initial.get("summary_only"):
                continue
            source_revision = initial["revision"]
            stream, inventory = receiver.snapshot()
            if not inventory or not stream:
                continue
            results = []
            stream.merge(method=0, fill_value=None)
            for raw in stream.select(component="Z").split():
                if raw.stats.endtime - raw.stats.starttime < 530:
                    continue
                try:
                    c = inventory.get_coordinates(raw.id, raw.stats.starttime)
                    delta = float(
                        distance(initial["lat"], initial["lon"], c["latitude"], c["longitude"])
                        / 111.195
                    )
                    arrivals = model.get_travel_times(
                        initial["depth_km"], delta, phase_list=["P", "S"]
                    )
                    p = initial["origin"] + min(a.time for a in arrivals if a.name == "P")
                    s = initial["origin"] + min(a.time for a in arrivals if a.name == "S")
                    tr = raw.copy().detrend("linear").taper(0.02)
                    from ..acquisition.fdsn import remove_response, clipped_counts

                    if clipped_counts(raw.data, raw.stats.sampling_rate):
                        results.append(dict(id=raw.id, accepted=False, reason="clipped"))
                        continue

                    remove_response(
                        tr,
                        inventory=inventory,
                        output="VEL",
                        pre_filt=(0.0005, 0.001, 1, 2),
                        water_level=60,
                    )
                    from ..models import Record
                    from ..processing.picking import pick

                    detector = raw.copy().detrend("linear").taper(0.02)
                    rate = detector.stats.sampling_rate
                    remove_response(
                        detector,
                        inventory=inventory,
                        output="VEL",
                        pre_filt=(0.02, 0.05, min(10, rate * 0.35), rate * 0.45),
                        water_level=60,
                    )
                    rec = Record(
                        raw.id[:-1],
                        c["latitude"],
                        c["longitude"],
                        float(raw.stats.starttime),
                        rate,
                        np.column_stack(
                            [detector.data, np.full(len(raw), np.nan), np.full(len(raw), np.nan)]
                        ),
                    )
                    candidates, method = pick(rec, "phasenet", "geofon")
                    candidates = [
                        v
                        for v in candidates
                        if v["phase"] == "P" and abs(v["time"] - p) < 15 and v["score"] >= 0.25
                    ]
                    if not candidates:
                        results.append(
                            dict(id=raw.id, accepted=False, reason="no-P-pick-near-theory")
                        )
                        continue
                    p = max(candidates, key=lambda v: v["score"])["time"]
                    result = measure_mwp(
                        tr.data,
                        tr.stats.sampling_rate,
                        float(tr.stats.starttime),
                        p,
                        s,
                        delta,
                        quality_velocity=detector.data,
                    )
                    result.update(
                        id=raw.id,
                        lat=c["latitude"],
                        lon=c["longitude"],
                        p_time=p,
                        timing="observed-PhaseNet-P; live proxy",
                    )
                    results.append(result)
                except Exception as exc:
                    results.append(dict(id=raw.id, accepted=False, reason=type(exc).__name__))
            mwp = aggregate_mwp(
                results,
                method="Tsuboi-physical-SI/HP0.008Hz/live-v2",
                source_hypocenter={k: initial[k] for k in ("origin", "lat", "lon", "depth_km")},
                picker="PhaseNet/geofon/2",
            )
            if mwp["value"] is None:
                continue
            with self.publication_lock:
                if self.stopping:
                    return
                event = self.store.get(eid)
                if event is None or event["revision"] != source_revision:
                    continue
                event["magnitudes"] = [
                    m for m in event.get("magnitudes", []) if m["type"] != "Mwp"
                ] + [mwp]
                saved = self.store.put(event, "live", eid, expected_revision=source_revision)
                if saved is None:
                    continue
                folder = self.settings.root / "runtime/reports"
                images = report_images(
                    saved, folder, travel_cache=self.settings.root / "runtime/iasp91.npz"
                )
                image = images["overview"]
                saved["closeup_image_file"] = str(images["closeup"])
                saved["image_file"] = str(image)
                with self.store.connect() as db:
                    payload = json.dumps(saved, ensure_ascii=False, allow_nan=False)
                    db.execute("UPDATE events SET payload=? WHERE id=?", (payload, eid))
                    self.store.update_summary(db, saved)
                    db.execute(
                        "UPDATE revisions SET payload=? WHERE event_id=? AND revision=?",
                        (payload, eid, saved["revision"]),
                    )
            receiver.terminate()
            with self.lock:
                self.far_jobs.pop(eid, None)
                self.far_completed.add(eid)

    async def far_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.update_far)
            except Exception as exc:
                self.messages.warning(
                    ("mwp", type(exc).__name__),
                    "背景 Mwp 更新失敗：%s",
                    type(exc).__name__,
                    exc_info=True,
                )
            await asyncio.sleep(10)

    async def run(self):
        for receiver in self.receivers:
            receiver.start()
        far_task = asyncio.create_task(self.far_loop()) if self.settings.live_mwp else None
        try:
            while True:
                try:
                    self.processing = True
                    await asyncio.to_thread(self.analyze)
                except Exception as exc:
                    self.error = type(exc).__name__
                    self.messages.warning(
                        ("regional", type(exc).__name__),
                        "Regional analysis failed: %s",
                        type(exc).__name__,
                        exc_info=True,
                    )
                finally:
                    self.processing = False
                await asyncio.sleep(self.settings.analysis_interval_s)
        finally:
            self.stopping = True
            if far_task:
                far_task.cancel()
                await asyncio.gather(far_task, return_exceptions=True)
            with self.lock:
                receivers = self.receivers + [v[0] for v in self.far_jobs.values()]
            for receiver in receivers:
                receiver.terminate()
            for receiver in self.receivers:
                if receiver.thread:
                    await asyncio.to_thread(receiver.thread.join, 2)
