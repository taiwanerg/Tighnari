"""Travel-time interfaces. Upstream data are loaded explicitly, never executed."""
import hashlib
import json
from pathlib import Path
import numpy as np
from scipy.interpolate import RegularGridInterpolator

EXPTECH_COMMIT = '1e59f06ea3a7f24334fc44cd029800f3e54aff02'
EXPTECH_URL = f'https://raw.githubusercontent.com/ExpTechTW/eq-travel-time/{EXPTECH_COMMIT}/time.json'


class HomogeneousTravelTime:
    name = 'homogeneous-vp6-vs3.5'
    max_depth = 100.0
    max_distance = float('inf')

    def times(self, depth_km, epicentral_km, phase='P', elevation_m=0):
        if phase not in ('P', 'S'):
            raise ValueError('phase must be P or S')
        speed = 6.0 if phase == 'P' else 3.5
        return np.hypot(epicentral_km, np.asarray(depth_km)+np.asarray(elevation_m)/1000)/speed


class TableTravelTime:
    """Bilinear interpolation with strict units/domain and no silent clamping."""
    def __init__(self, path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        doc = json.loads(raw)
        meta = doc.get('meta', {})
        if meta.get('unit') != 's' or meta.get('dist') != 'epicentral_km':
            raise ValueError('走時表必須使用秒與震央距 km')
        self.depth = np.asarray(doc['depth'], dtype=float)
        self.dist = np.asarray(doc['dist'], dtype=float)
        for axis in (self.depth, self.dist):
            if axis.ndim != 1 or len(axis)<2 or not np.isfinite(axis).all() or np.any(np.diff(axis)<=0) or axis[0]<0:
                raise ValueError('走時座標必須為非負、有限、嚴格遞增網格')
        p, sp = np.asarray(doc['p'], float), np.asarray(doc['sp'], float)
        shape = (len(self.depth), len(self.dist))
        if p.shape != shape or sp.shape != shape or not np.isfinite(p).all() or not np.isfinite(sp).all() or np.any(p<0) or np.any(sp<0):
            raise ValueError('走時陣列形狀或數值無效')
        self.max_depth = float(self.depth[-1]); self.max_distance = float(self.dist[-1])
        self.name = 'table:'+meta.get('project', 'custom')+':'+self.sha256[:12]
        self._p = RegularGridInterpolator((self.depth, self.dist), p, bounds_error=False, fill_value=np.nan)
        self._s = RegularGridInterpolator((self.depth, self.dist), p+sp, bounds_error=False, fill_value=np.nan)

    def times(self, depth_km, epicentral_km, phase='P', elevation_m=0):
        if phase not in ('P', 'S'):
            raise ValueError('phase must be P or S')
        depth, dist = np.broadcast_arrays(np.asarray(depth_km, float), np.asarray(epicentral_km, float))
        pts = np.column_stack((depth.ravel(), dist.ravel()))
        value = (self._p if phase == 'P' else self._s)(pts).reshape(depth.shape)
        # Approximate elevation correction; table has sea-level receivers.
        value = value + np.asarray(elevation_m)/1000/(6.0 if phase=='P' else 3.5)
        return value


def download_exptech_table(path):
    import httpx
    path = Path(path)
    response = httpx.get(EXPTECH_URL, timeout=30, follow_redirects=True)
    response.raise_for_status()
    if len(response.content)>2_000_000:
        raise ValueError('走時表過大')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    try:
        temp.write_bytes(response.content)
        model=TableTravelTime(temp)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    provenance=dict(repository='https://github.com/ExpTechTW/eq-travel-time',commit=EXPTECH_COMMIT,
                    url=EXPTECH_URL,sha256=model.sha256,
                    attribution='ExpTech / YuYu1015',license='No license file found at the pinned commit; downloaded for local evaluation, not bundled as package data.')
    path.with_suffix('.provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf-8')
    return TableTravelTime(path)


def wave_radius(model,depth_km,elapsed_s,phase):
    if elapsed_s<0:return None
    r=np.linspace(0,min(600,model.max_distance),601)
    t=model.times(depth_km,r,phase=phase)
    if not np.isfinite(t).all() or np.any(np.diff(t)<0) or elapsed_s<t[0] or elapsed_s>t[-1]:return None
    return float(np.interp(elapsed_s,t,r))
