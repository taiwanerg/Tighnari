"""Geographic station discovery and fault-isolated, reproducible FDSN downloads."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from .location import distance
from .providers import fdsn_fetch

BASE='https://service.earthscope.org'


def discover(start,end,base=BASE,network='TW,IU',bounds=(21,26.8,118,124),limit=12):
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client
    lo,hi,left,right=bounds
    if not (20<=lo<hi<=27 and 117<=left<right<=125):
        raise ValueError('台灣周邊範圍需位於 20–27N、117–125E 且上下界遞增')
    inv=Client(base,timeout=30).get_stations(network=network,minlatitude=lo,maxlatitude=hi,
        minlongitude=left,maxlongitude=right,starttime=UTCDateTime(start),endtime=UTCDateTime(end),
        channel='HN?,HH?,BH?',level='channel',includerestricted=False)
    rows=[]
    for net in inv:
        for st in net:
            if not any(c.sample_rate>=20 for c in st):continue
            rows.append(dict(network=net.code,station=st.code,lat=st.latitude,lon=st.longitude,
                             elevation_m=st.elevation,channels=sorted({c.location_code+'.'+c.code for c in st})))
    # Stable ordering about the bbox center; no earthquake catalog coordinates.
    rows.sort(key=lambda s:(float(distance((lo+hi)/2,(left+right)/2,s['lat'],s['lon'])),s['network'],s['station']))
    # Keep co-located network alternatives at discovery: metadata availability
    # does not imply waveform availability. The engine deduplicates actual votes.
    return rows[:limit]



def download_dataset(output,start,end,base=BASE,network='TW,IU',bounds=(21,26.8,118,124),limit=12,workers=3):
    from obspy import UTCDateTime
    if not 0<float(UTCDateTime(end)-UTCDateTime(start))<=600:raise ValueError('下載時段必須在 0 到 600 秒之間')
    if not 1<=limit<=30 or not 1<=workers<=4:raise ValueError('測站數限 1–30，下載並行數限 1–4')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    selected=discover(start,end,base,network,bounds,limit)
    request=dict(base=base,network=network,start=str(UTCDateTime(start)),end=str(UTCDateTime(end)),bounds=list(bounds))
    cache_key=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()[:16]
    cache=output/'cache'/cache_key;cache.mkdir(parents=True,exist_ok=True)
    manifest=dict(source='IRIS / EarthScope FDSN',request=request,
        acquisition_utc=datetime.now(timezone.utc).isoformat(),selected=selected,stations=[],errors=[])
    packets=[]
    def fetch(row):
        key=row['network']+'.'+row['station'];path=cache/(key+'.jsonl')
        cached=path.exists()
        if cached:
            from .replay import read_packets
            data=list(read_packets(path))
        else:
            data=fdsn_fetch(row['network'],row['station'],start,end,base)
            if not data:raise ValueError('沒有可校正的完整三軸連續片段')
            tmp=path.with_suffix('.tmp')
            tmp.write_text(''.join(p.model_dump_json()+'\n' for p in data),encoding='utf-8');tmp.replace(path)
        return data,dict(network=row['network'],station=row['station'],cached=cached,
            packet_count=len(data),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            streams=sorted({p.station.id for p in data}),start=min(p.start for p in data),
            end=max(p.start+len(p.samples)/p.rate for p in data),
            requested_start=float(UTCDateTime(start)),requested_end=float(UTCDateTime(end)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs={pool.submit(fetch,row):row for row in selected}
        for job in as_completed(jobs):
            row=jobs[job]
            try:
                data,record=job.result();packets.extend(data);manifest['stations'].append(record)
                print(f"{row['network']}.{row['station']}: {len(data)} packets"+(' (cache)' if record['cached'] else ''),flush=True)
            except Exception as exc:
                manifest['errors'].append(dict(network=row['network'],station=row['station'],error=type(exc).__name__,detail=str(exc)[:500]))
                print(f"{row['network']}.{row['station']}: skipped ({type(exc).__name__})",flush=True)
    packets.sort(key=lambda p:(p.start,p.station.id))
    manifest['stations'].sort(key=lambda r:(r['network'],r['station']))
    manifest['errors'].sort(key=lambda r:(r['network'],r['station']))
    target=output/'waveforms.jsonl'
    target.write_text(''.join(p.model_dump_json()+'\n' for p in packets),encoding='utf-8')
    manifest['waveform_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
    manifest['station_count']=len({p.station.id for p in packets})
    independent=[]
    for p in packets:
        if all(distance(p.station.lat,p.station.lon,s.lat,s.lon)>2 for s in independent):independent.append(p.station)
    manifest['independent_sites']=len(independent)
    manifest['location_ready']=len(independent)>=4
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return target,manifest
