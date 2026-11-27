"""SeedLink acquisition; FDSN is used only for station/response metadata."""
import asyncio
import os
from pathlib import Path
import threading
import time
import numpy as np
from .models import Station, WavePacket
from .reporting import Reports


def calibrate(stream, inventory):
    """Reject incomplete/gapped axes and retain 5s padding at both edges."""
    packets = []
    for net, sta in sorted({(tr.stats.network,tr.stats.station) for tr in stream}):
        accepted = False
        for prefix in ('HN','HH','BH'):
            locations = sorted({tr.stats.location for tr in stream.select(network=net,station=sta,channel=prefix+'?')})
            for loc in locations:
                group = stream.select(network=net,station=sta,location=loc,channel=prefix+'?').copy()
                group.merge(method=0,fill_value=None)
                if len(group)!=3 or group.get_gaps() or any(np.ma.isMaskedArray(tr.data) and np.ma.getmaskarray(tr.data).any() for tr in group):
                    continue
                rates = {tr.stats.sampling_rate for tr in group}
                if len(rates)!=1 or not 20<=min(rates)<=500:
                    continue
                rate = min(rates)
                start,end = max(tr.stats.starttime for tr in group),min(tr.stats.endtime for tr in group)
                if end-start<40:
                    continue
                group.trim(start,end,nearest_sample=False)
                if len({len(tr) for tr in group})!=1 or any(abs(float(tr.stats.starttime-group[0].stats.starttime))*rate>.1 for tr in group):
                    continue
                try:
                    c = inventory.get_coordinates(group[0].id,group[0].stats.starttime)
                    group.detrend('linear').taper(.05)
                    group.remove_response(inventory=inventory,output='ACC',pre_filt=(.05,.1,min(10,rate*.35),rate*.45),water_level=60)
                    group.rotate(method='->ZNE',inventory=inventory)
                    axes = [group.select(component=c) for c in 'ZNE']
                    if any(len(a)!=1 for a in axes):
                        continue
                    station = Station(id=f'{net}.{sta}.{loc}.{prefix}',name=sta,lat=c['latitude'],lon=c['longitude'],
                        elevation_m=c['elevation'],source='IRIS / EarthScope SeedLink')
                    pad = int(5*rate)
                    samples = np.column_stack([a[0].data[pad:-pad]*100 for a in axes])
                    for offset in range(0,len(samples),int(30*rate)):
                        packets.append(WavePacket(station=station,start=float(group[0].stats.starttime)+5+offset/rate,
                            rate=rate,samples=samples[offset:offset+int(30*rate)].tolist()))
                    accepted = True
                    break
                except (ValueError,KeyError,TypeError,NotImplementedError):
                    continue
            if accepted:
                break
    return sorted(packets,key=lambda p:(p.start,p.station.id))


class SeedLinkSource:
    def __init__(self,base,server,network,limit):
        from obspy import Stream
        self.base,self.server,self.network,self.limit = base,server,network,limit
        self.stream = Stream()
        self.inventory = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.client = None
        self.status = dict(state='取得測站響應資料',count=0,updated=None)

    def on_data(self,trace):
        from obspy import UTCDateTime
        now = time.time()
        if abs(now-float(trace.stats.endtime))>240:
            return
        with self.lock:
            self.stream += trace.copy()
            self.stream.trim(UTCDateTime(now-240))
            self.stream.merge(method=0,fill_value=None)
            self.status = dict(state='接收 SeedLink 波形',updated=now,
                count=len({(t.stats.network,t.stats.station) for t in self.stream}))

    def run(self):
        from obspy import UTCDateTime
        from obspy.clients.fdsn import Client
        from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient
        delay = 2
        while not self.stop.is_set():
            try:
                inventory = Client(self.base,timeout=30).get_stations(network=self.network,
                    minlatitude=21,maxlatitude=26.8,minlongitude=118,maxlongitude=124,
                    starttime=UTCDateTime(),channel='HN?,HH?,BH?',level='response',includerestricted=False)
                client = EasySeedLinkClient(self.server,autoconnect=False)
                self.client = client
                client.conn.timeout = 15
                client.conn.set_net_timeout(30)
                client.on_data = self.on_data
                def stream_error():
                    self.status['state'] = 'SeedLink 連線錯誤；重連中'
                client.on_seedlink_error = stream_error
                client.connect()
                import xml.etree.ElementTree as ET
                info = ET.fromstring(client.get_info('STREAMS'))
                available = {(s.get('network'),s.get('name')):
                    {(c.get('location',''),c.get('seedname','')) for c in s.findall('stream')}
                    for s in info.findall('station')}
                selected = []
                for net in inventory:
                    for sta in net:
                        channels = available.get((net.code,sta.code),set())
                        groups = {(c.location_code,c.code[:2]) for c in sta if c.sample_rate>=20 and (c.location_code,c.code) in channels}
                        groups = {(loc,prefix) for loc,prefix in groups if len({c.code for c in sta if c.location_code==loc and c.code.startswith(prefix) and (loc,c.code) in channels})>=3}
                        if groups:
                            selected.append((net.code,sta.code,groups))
                selected.sort(key=lambda s:(s[0],s[1]))
                selected = selected[:self.limit]
                if not selected:
                    raise ValueError('SeedLink 無可用的區域三軸站')
                with self.lock:
                    self.inventory = inventory
                for net,sta,groups in selected:
                    for loc,prefix in sorted(groups):
                        client.select_stream(net,sta,loc+prefix+'?.D')
                self.status['state'] = f'SeedLink 已選取 {len(selected)} 站，等待波形'
                if self.stop.is_set():
                    break
                delay = 2
                client.run()
            except Exception as exc:
                self.status['state'] = '資料來源失敗：'+type(exc).__name__+'；重試中'
            finally:
                if self.client:
                    self.client.close()
                self.client = None
            self.stop.wait(delay)
            delay = min(60,delay*2)

    def snapshot(self):
        with self.lock:
            return self.stream.copy(),self.inventory

    def terminate(self):
        self.stop.set()
        if self.client:
            self.client.conn.terminate()


class LocationMonitor:
    def __init__(self,model,directory='runtime/reports',webhook=None):
        self.model = model
        self.reports = Reports(directory,webhook)
        self.source = SeedLinkSource(os.getenv('SAIU_FDSN_BASE','https://service.earthscope.org'),
            os.getenv('SAIU_SEEDLINK_SERVER','rtserve.earthscope.org:18000'),
            os.getenv('SAIU_NETWORK','TW,IU'),int(os.getenv('SAIU_MAX_STATIONS','20')))
        if not 1<=self.source.limit<=30:
            raise ValueError('SAIU_MAX_STATIONS 必須為 1–30')
        self.latest = None
        self.error = None

    def analyze(self):
        from .replay import analyze_packets
        from .magnitude import local_magnitude
        stream,inventory = self.source.snapshot()
        if inventory is None or not stream:
            self.reports.deliver()
            return
        packets = calibrate(stream,inventory)
        if not packets:
            self.error = '尚無可校正的連續三軸波形；暖機或等待缺軸測站'
            self.reports.deliver()
            return
        report = analyze_packets(packets,self.model)
        snapshot = report['snapshot']
        for item in report['events']:
            event = item['last']
            if time.time()-event['origin']<90 or event['count']<5 or event['degrees_of_freedom']<1:
                continue
            event.update(local_magnitude(packets,event,self.model))
            self.reports.record(event,snapshot['stations'])
        self.latest = snapshot
        self.error = None
        self.reports.deliver()

    async def run(self):
        thread = threading.Thread(target=self.source.run,daemon=True,name='iris-seedlink')
        thread.start()
        try:
            while True:
                try:
                    await asyncio.to_thread(self.analyze)
                except Exception as exc:
                    self.error = '分析失敗：'+type(exc).__name__
                await asyncio.sleep(15)
        finally:
            self.source.terminate()
            await asyncio.to_thread(thread.join,2)

    def state(self):
        from .engine import Engine
        state = dict(self.latest or Engine('iris',self.model).snapshot())
        now = time.time()
        state.update(mode='iris',now=now,regions=[],wave_radii={})
        state['stations'] = [dict(s,age_s=max(0,now-s['end']),online=now-s['end']<90) for s in state['stations']]
        state['event'] = self.reports.rows[-1]['event'] if self.reports.rows else None
        state['reports'] = [dict(event=r['event'],sent=r['sent'],image='/reports/'+Path(r['image']).name,
                                error=r.get('error')) for r in self.reports.rows[-30:]][::-1]
        state['sources'] = {'IRIS / EarthScope SeedLink':dict(self.source.status)}
        state['playback'] = dict(playing=True,finished=False,error=self.error)
        state['notification'] = dict(enabled=bool(self.reports.webhook),pending=sum(not r['sent'] for r in self.reports.rows))
        return state
