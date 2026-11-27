"""One chronological packet iterator shared by offline analysis and the UI."""
import heapq
import math
import json
from pathlib import Path
from .models import WavePacket


def read_packets(path):
    with Path(path).open(encoding='utf-8') as stream:
        for number,line in enumerate(stream,1):
            if line.strip():
                try: yield WavePacket.model_validate_json(line)
                except ValueError as exc: raise ValueError(f'{path}:{number}: invalid waveform packet') from exc


def iter_seconds(packets):
    heap=[(p.start,i,0,p) for i,p in enumerate(packets)]
    heapq.heapify(heap)
    while heap:
        start,i,offset,p=heapq.heappop(heap)
        step=max(1,int(round(p.rate)))
        yield p.model_copy(update={'start':start,'samples':p.samples[offset:offset+step]})
        offset+=step
        if offset<len(p.samples):
            heapq.heappush(heap,(p.start+offset/p.rate,i,offset,p))


def analyze(path, travel_model=None):
    return analyze_packets(list(read_packets(path)), travel_model, str(Path(path)))


def analyze_packets(packets, travel_model=None, input_name='SeedLink'):
    from .engine import Engine
    if not packets: raise ValueError('沒有波形封包')
    engine=Engine('replay',travel_model)
    engine.now=min(p.start for p in packets)
    revisions=[]; picks={}; seen_revision=None
    for p in iter_seconds(packets):
        engine.ingest(p)
        for key,pick in engine.picks.items():
            stamp=(key,pick['time'])
            if stamp not in picks: picks[stamp]=dict(pick,detected_at=engine.now)
        if engine.event:
            key=(engine.event['id'],engine.event['revision'])
            if key!=seen_revision:
                revisions.append(dict(engine.event));seen_revision=key
    from .travel import HomogeneousTravelTime
    model=travel_model or HomogeneousTravelTime()
    from .location import distance
    events=[]
    for eid in dict.fromkeys(e['id'] for e in revisions):
        rows=[e for e in revisions if e['id']==eid]
        # Retain both first and last solutions. No reference catalog is used here.
        final=rows[-1];diagnostics=[]
        for pick in picks.values():
            if not 0<=pick['time']-final['origin']<=100:continue
            tt=float(model.times(final['depth_km'],distance(final['lat'],final['lon'],pick['lat'],pick['lon']),elevation_m=pick.get('elevation_m',0)))
            if not math.isfinite(tt):
                diagnostics.append(dict(id=pick['id'],observed=pick['time'],predicted=None,residual_s=None,used=False,reason='outside-travel-model'))
                continue
            residual=final['origin']+tt-pick['time']
            used=pick['id'] in final['used']
            diagnostics.append(dict(id=pick['id'],observed=pick['time'],predicted=final['origin']+tt,
                                    residual_s=residual,used=used,
                                    reason='accepted' if used else 'arrival-residual-exceeded' if abs(residual)>=1 else 'not-in-final-association'))
        events.append(dict(id=eid,first=rows[0],first_confirmed=next((r for r in rows if r['quality']=='自動定位'),None),last=final,revisions=len(rows),arrival_diagnostics=diagnostics))
    return dict(mode='waveform-location',input=input_name,
                travel_model=travel_model.name if travel_model else 'homogeneous-vp6-vs3.5',
                start=min(p.start for p in packets),end=max(p.start+len(p.samples)/p.rate for p in packets),
                station_count=len({p.station.id for p in packets}),packets=engine.packets,rejected=engine.rejected,
                picks=list(picks.values()),events=events,revisions=revisions,
                snapshot=engine.snapshot())


def compare_catalog(report, catalog):
    """Independent post-analysis evaluation only; never called by the locator."""
    from .location import distance
    reference=[]
    for feature in catalog.get('features',[]):
        lon,lat,depth=feature['geometry']['coordinates'][:3]
        reference.append(dict(id=feature['id'],lat=lat,lon=lon,depth_km=depth,
                              origin=feature['properties']['time']/1000,url=feature['properties'].get('url')))
    matches=[]
    for event in report['events']:
        for stage in ('first','first_confirmed','last'):
            result=event.get(stage)
            if result is None:continue
            if not reference: continue
            nearest=min(reference,key=lambda r:abs(r['origin']-result['origin']))
            delta=result['origin']-nearest['origin']
            if abs(delta)>30:
                matches.append(dict(event=event['id'],stage=stage,matched=False));continue
            matches.append(dict(event=event['id'],stage=stage,matched=True,reference=nearest,
                                epicentral_error_km=float(distance(result['lat'],result['lon'],nearest['lat'],nearest['lon'])),
                                depth_error_km=result['depth_km']-nearest['depth_km'],origin_error_s=delta,
                                report_delay_s=result['updated']-nearest['origin']))
    return matches
