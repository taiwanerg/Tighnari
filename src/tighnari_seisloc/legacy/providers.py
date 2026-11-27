"""Public summary adapters and calibrated FDSN waveform input."""
import asyncio
import base64
import csv
import gzip
import io
import json
import time
import httpx
from .models import Station, WavePacket
from .signal import intensity

TREM_STATIONS='https://static.core-tnn1.exptech.dev/resource/station'
TREM_STREAM='https://api.lb-tpe1.exptech.dev/api/v1/trem/sse?topics=trem.rts.v1&mode=live'
RF_PGA='https://rptes.com/api/RFEQ/pga'


def parse_rf(raw, now):
    rows=[]
    for s in raw.get('data',[]):
        try:
            ts=float(s['timestamp'])/1000
            if not -5 <= now-ts <= 30:
                continue
            station=Station(id='RF.'+str(s['id']),name=s.get('cname') or s['name'],lat=s['lat'],lon=s['lon'],source='RPTeS')
            pga=float(s['pga'])
            if not 0 <= pga <= 20000:
                continue
            rows.append(dict(**station.model_dump(),end=ts,pga=pga,pgv=None,intensity=None,
                             reported_intensity=str(s.get('shindo_15','—')),kind='summary',waveform=[],picked=False))
        except (KeyError,ValueError,TypeError):
            continue
    return rows


def parse_trem(raw, metadata, now):
    ts=float(raw['ts'])/1000
    if not -5 <= now-ts <= 15:
        return []
    rows=[]
    for sid,s in raw.get('stations',{}).items():
        if sid not in metadata:
            continue
        try:
            st=metadata[sid]
            pga=float(s['pga']) if s.get('pga') is not None else None
            if pga is not None and not 0 <= pga <= 20000:
                continue
            rows.append(dict(**st.model_dump(),end=ts,pga=pga,pgv=None,intensity=None,
                             reported_intensity=str(s.get('i','—')),kind='summary',waveform=[],picked=False))
        except (ValueError,TypeError):
            continue
    return rows


class SummarySources:
    def __init__(self):
        self.rows={}; self.status={k:dict(state='等待連線',updated=None,count=0) for k in ('TREM','RPTeS')}

    async def rf(self):
        async with httpx.AsyncClient(timeout=15,follow_redirects=True) as client:
            while True:
                try:
                    r=await client.get(RF_PGA); r.raise_for_status()
                    rows=parse_rf(r.json(),time.time())
                    self.rows['RPTeS']=rows
                    self.status['RPTeS']=dict(state='摘要連線' if rows else '無新資料',updated=time.time(),count=len(rows))
                except Exception as exc:
                    self.status['RPTeS']['state']='連線失敗：'+type(exc).__name__
                await asyncio.sleep(10)

    async def trem(self):
        delay=2
        async with httpx.AsyncClient(timeout=httpx.Timeout(30,read=45),follow_redirects=True) as client:
            while True:
                try:
                    r=await client.get(TREM_STATIONS); r.raise_for_status()
                    metadata={}
                    for s in csv.DictReader(io.StringIO(r.text)):
                        try:
                            metadata[s['id']]=Station(id='TREM.'+s['id'],name='TREM '+s['code'],lat=s['lat'],lon=s['lon'],source='TREM')
                        except (KeyError,ValueError):
                            continue
                    event=''; data=[]
                    async with client.stream('GET',TREM_STREAM) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if line.startswith('event:'): event=line[6:].strip()
                            elif line.startswith('data:'): data.append(line[5:].strip())
                            elif not line:
                                if event=='trem.rts.v1' and data:
                                    compressed=base64.b64decode(''.join(data),validate=True)
                                    # Limit decompression to 2 MB.
                                    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as gz:
                                        decoded=gz.read(2_000_001)
                                    if len(decoded)>2_000_000: raise ValueError('oversized frame')
                                    rows=parse_trem(json.loads(decoded),metadata,time.time())
                                    self.rows['TREM']=rows
                                    self.status['TREM']=dict(state='摘要連線' if rows else '無新資料',updated=time.time(),count=len(rows))
                                    delay=2
                                event=''; data=[]
                    raise ConnectionError('stream ended')
                except Exception as exc:
                    self.status['TREM']['state']='連線失敗：'+type(exc).__name__
                await asyncio.sleep(delay); delay=min(60,delay*2)


def fdsn_fetch(network, station, start, end, base='https://service.earthscope.org'):
    """Historical/near-real-time FDSN; no promise of low-latency availability.

    Three calibrated axes are required; gaps and incomplete orientations are skipped.
    Uses full instrument response (ACC output), not sensitivity-only scaling.
    """
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client
    client=Client(base,timeout=25)
    begin,finish=UTCDateTime(start),UTCDateTime(end)
    if not 0 < float(finish-begin) <= 600:
        raise ValueError('FDSN 每次下載範圍必須在 0 到 600 秒之間')
    inv=client.get_stations(network=network,station=station,location='*',channel='HN?,HH?,BH?',
                            starttime=begin,endtime=finish,level='response')
    stream=client.get_waveforms(network,station,'*','HN?,HH?,BH?',begin,finish,attach_response=False)
    packets=[]; seen=set()
    # Prefer accelerometer, then high-rate and broadband; do not combine sensor families.
    for net in inv:
        for sta in net:
            for prefix in ('HN','HH','BH'):
                for loc in sorted({tr.stats.location for tr in stream.select(network=net.code,station=sta.code,channel=prefix+'?')}):
                    sid=f'{net.code}.{sta.code}.{loc}.{prefix}'
                    site=f'{net.code}.{sta.code}'
                    if site in seen: continue
                    group=stream.select(network=net.code,station=sta.code,location=loc,channel=prefix+'?').copy()
                    if len(group)!=3 or group.get_gaps(): continue
                    rates={tr.stats.sampling_rate for tr in group}
                    if len(rates)!=1 or min(rates)<20: continue
                    rate=min(rates)
                    if any(abs(float(tr.stats.starttime-group[0].stats.starttime))*rate > .1 for tr in group): continue
                    group.detrend('linear'); group.taper(.05)
                    group.remove_response(inventory=inv,output='ACC',pre_filt=(.05,.1,min(10,rate*.35),rate*.45),water_level=60)
                    group.rotate(method='->ZNE',inventory=inv)
                    axes=[group.select(component=c) for c in 'ZNE']
                    if any(len(x)!=1 for x in axes): continue
                    import numpy as np
                    n=min(len(x[0]) for x in axes)
                    data=np.column_stack([x[0].data[:n]*100 for x in axes])
                    st=Station(id=sid,name=sta.code,lat=sta.latitude,lon=sta.longitude,
                               elevation_m=sta.elevation,source='IRIS / EarthScope')
                    for offset in range(0,n,int(rate*30)):
                        packets.append(WavePacket(station=st,start=float(group[0].stats.starttime)+offset/rate,
                                                  rate=rate,samples=data[offset:offset+int(rate*30)].tolist()))
                    seen.add(site)
    return sorted(packets,key=lambda p:p.start)


def rf_normalize(raw, station, scale_to_cm_s2, axes=('z','y','x')):
    """Explicit calibration/orientation contract, jitter resampling, gap splitting.

    axes describes raw fields in geographic Z,N,E order, supplied from installation
    metadata. We never infer raw units, vertical direction or orientation from names.
    """
    import math
    import numpy as np
    if not math.isfinite(scale_to_cm_s2) or not 0 < scale_to_cm_s2 <= 10000:
        raise ValueError('Invalid calibration scale')
    if sorted(axes)!=['x','y','z']:
        raise ValueError('axes must be a permutation of x,y,z')
    samples=[s for s in raw if str(s['station_id'])==station.id.removeprefix('RF.')]
    if len(samples)<40: return []
    ts=np.array([float(s['unixTimestamp'])/1000 for s in samples])
    values=np.array([[float(s[k])*scale_to_cm_s2 for k in axes] for s in samples])
    if not np.isfinite(ts).all() or not np.isfinite(values).all() or np.any(np.diff(ts)<=0):
        raise ValueError('波形時間必須遞增且不可重複')
    dt=float(np.median(np.diff(ts))); rate=round(1/dt,3)
    if not 20 <= rate <= 500: raise ValueError('不支援的取樣率')
    cuts=np.r_[0,np.where(np.diff(ts)>dt*1.8)[0]+1,len(ts)]
    packets=[]
    for lo,hi in zip(cuts[:-1],cuts[1:]):
        if hi-lo < rate: continue
        t=ts[lo:hi]; v=values[lo:hi]
        grid=t[0]+np.arange(int(np.floor((t[-1]-t[0])*rate))+1)/rate
        a=np.column_stack([np.interp(grid,t,v[:,i]) for i in range(3)])
        for offset in range(0,len(grid),int(rate*30)):
            packets.append(WavePacket(station=station,start=float(grid[offset]),rate=rate,
                                      samples=a[offset:offset+int(rate*30)].tolist()))
    return packets
