from collections import deque
import time
import numpy as np
from .signal import features, pick_p, intensity, initial_pd
from .location import locate, distance, estimate_magnitude, predict_pga, VS
from .regions import REGIONS


class Engine:
    def __init__(self, mode='live', travel_model=None):
        self.mode = mode
        self.travel_model=travel_model
        self.buffers = {}; self.stations = {}; self.picks = {}
        self.event = None; self.history = deque(maxlen=100)
        self.now = time.time(); self.rejected = 0; self.packets = 0
        self.last_analysis = float("-inf"); self.last_pick = {}
        self.location_cache = {}

    def ingest(self, packet):
        st = packet.station; sid = st.id; rate = packet.rate
        end = packet.start + len(packet.samples)/rate
        buf = self.buffers.get(sid)
        if buf and packet.start < buf['end'] - .5/rate:
            self.rejected += 1
            return False  # duplicates, overlap and out-of-order packets do not trigger
        if not buf or abs(packet.start-buf['end']) > 1.5/rate or rate != buf['rate']:
            buf = dict(start=packet.start, end=packet.start, rate=rate, a=np.empty((0,3)))
            self.picks.pop(sid, None)
        a = np.asarray(packet.samples)
        if len(a) > rate and np.max(np.std(a, axis=0)) < 1e-9:
            self.rejected += 1
            self.buffers.pop(sid, None); self.picks.pop(sid, None)
            return False
        buf['a'] = np.concatenate((buf['a'], a))
        trim = max(0, len(buf['a'])-int(rate*90))
        buf['a'] = buf['a'][trim:]; buf['start'] += trim/rate; buf['end'] = end
        self.buffers[sid] = buf; self.now = max(self.now, end); self.packets += 1
        protected=set(self.event['used']) if self.event and self.now-self.event['origin']<120 else set()
        self.picks = {k:v for k,v in self.picks.items() if self.now-v['time'] < 45 or k in protected}
        pga, pgv = features(buf['a'], rate)
        recent = buf['a'][-int(rate*20):]
        row = dict(**st.model_dump(), end=end, pga=pga, pgv=pgv, intensity=intensity(pga,pgv),
                   waveform=recent[::max(1,int(rate/20)),0].round(4).tolist(), kind='waveform')
        self.stations[sid] = row
        if sid not in self.picks:
            offset = pick_p(buf['a'], rate, since=max(0,self.last_pick.get(sid,0)+45-buf['start']))
            if offset is not None and self.now-(buf['start']+offset) < 45:
                self.picks[sid] = dict(**st.model_dump(), time=buf['start']+offset)
                self.last_pick[sid] = buf['start']+offset
        if sid in self.picks:
            row['pd_cm']=initial_pd(buf['a'],rate,self.picks[sid]['time']-buf['start'])
        self._associate()
        # Bounded retention, including disconnected stations.
        for key in list(self.buffers):
            if self.now-self.buffers[key]['end'] > 180:
                del self.buffers[key]; self.stations.pop(key, None); self.last_pick.pop(key, None)
        return True

    def _associate(self):
        if len(self.picks) < 4 or self.now-self.last_analysis < 1:
            return
        self.last_analysis = self.now
        candidates = sorted(self.picks.values(), key=lambda p:p['time'])
        # At most one independent trigger per site, including co-located providers.
        unique=[]
        for p in candidates:
            if all(distance(p['lat'],p['lon'],q['lat'],q['lon']) > 2 for q in unique):
                unique.append(p)
        if len(unique) < 4:
            return
        # Try distinct compatible groups so one early noise trigger cannot anchor
        # the entire network. This remains a single-event research associator.
        solutions=[]; seen=set()
        for seed in unique[:24]:
            cluster=[p for p in unique if abs(p['time']-seed['time']) <= distance(p['lat'],p['lon'],seed['lat'],seed['lon'])/4+2]
            key=tuple(p['id'] for p in cluster)
            if len(cluster)<4 or key in seen: continue
            seen.add(key)
            signature=tuple((p['id'],p['time']) for p in cluster)
            if signature not in self.location_cache:
                if len(self.location_cache)>=32:self.location_cache.clear()
                self.location_cache[signature]=locate(cluster,self.travel_model)
            cached=self.location_cache[signature]
            result=dict(cached) if cached else None
            if result and result['rms_s'] <= .8:
                solutions.append((result,seed))
        if not solutions: return
        result,seed=max(solutions,key=lambda pair:(pair[0]['count'],-pair[0]['rms_s']))
        # EPIC-inspired nearby-station participation, only continuous waveform sites.
        nearby=[s for s in self.stations.values() if self.now-s['end'] < 5 and distance(seed['lat'],seed['lon'],s['lat'],s['lon']) < 80]
        participation = sum(s['id'] in result['used'] for s in nearby)/max(1,len(nearby))
        if nearby and participation < .4:
            return
        same_event=self.event and abs(self.event['origin']-result['origin'])<20
        if same_event and result['count']<self.event['count']:
            return  # Evidence aging must not degrade an established solution.
        result['participation'] = participation
        result['magnitude'], result['magnitude_count'] = estimate_magnitude(self.stations.values(), result)
        if same_event and result['magnitude'] is None and self.event['magnitude'] is not None:
            result['magnitude']=self.event['magnitude']
            result['magnitude_count']=self.event['magnitude_count']
        result['magnitude_method']='Taiwan NACB Pd / Wu et al. 2006'
        result['magnitude_saturation']=result['magnitude'] is not None and result['magnitude']>=6.5
        result['quality'] = '自動定位' if result['count'] >= 5 and result['gap_deg'] < 300 and result['horizontal_sigma_km'] < 50 else '定位待確認'
        result['updated'] = self.now
        if self.event and abs(self.event['origin']-result['origin']) < 20:
            result['id']=self.event['id']; result['revision']=self.event['revision']+1
        else:
            result['id']=f"{self.mode}-{int(result['origin']*1000)}"; result['revision']=1
        self.event=result
        self.history.append(dict(result))

    def snapshot(self, now=None):
        now = self.now if self.mode != 'live' else (now or time.time())
        event = self.event if self.event and (self.mode!='live' or now-self.event['origin'] < 120) else None
        rows = [dict(s, age_s=max(0,now-s['end']), online=now-s['end'] < 15,
                     picked=s['id'] in self.picks) for s in self.stations.values()]
        regions=[]
        if event and event['magnitude'] is not None:
            for name, lat, lon in REGIONS:
                r=float(np.hypot(distance(lat,lon,event['lat'],event['lon']),event['depth_km']))
                pga=predict_pga(event['magnitude'], r)
                from .travel import HomogeneousTravelTime
                tt=(self.travel_model or HomogeneousTravelTime()).times(event['depth_km'],distance(lat,lon,event['lat'],event['lon']),phase='S')
                if not np.isfinite(tt): continue
                regions.append(dict(name=name,lat=lat,lon=lon,pga=pga,intensity=intensity(pga),
                                    strong=pga>=80,arrival=event['origin']+float(tt),
                                    seconds=round(event['origin']+float(tt)-now,1)))
        from .travel import HomogeneousTravelTime, wave_radius
        model=self.travel_model or HomogeneousTravelTime()
        radii={phase:wave_radius(model,event['depth_km'],now-event['origin'],phase) for phase in ('P','S')} if event else {}
        return dict(travel_model=model.name,wave_radii=radii,mode=self.mode, now=now, event=event, stations=rows, regions=regions,
                    history=list(self.history)[-30:], packets=self.packets, rejected=self.rejected,
                    picks=len(self.picks))
