"""Deterministic synthetic input, routed through the same production signal pipeline."""
import numpy as np
from .models import Station, WavePacket
from .location import distance, VP, VS

TRUTH = dict(lat=23.84,lon=121.65,depth_km=18,magnitude=5.8)
SITES=[('花蓮',23.98,121.60),('壽豐',23.87,121.51),('鳳林',23.75,121.45),('光復',23.67,121.42),
('瑞穗',23.50,121.37),('玉里',23.34,121.31),('長濱',23.32,121.45),('成功',23.10,121.37),
('臺東',22.76,121.15),('宜蘭',24.75,121.75),('南澳',24.46,121.80),('羅東',24.68,121.77),
('臺北',25.04,121.56),('新竹',24.81,120.97),('臺中',24.16,120.65),('埔里',23.97,120.97),
('南投',23.91,120.69),('嘉義',23.48,120.45),('臺南',23.00,120.21),('高雄',22.63,120.30)]


def make_demo(start, seconds=90, rate=50, noise_only=False, spike=False):
    rng=np.random.default_rng(20261101)
    arrays=[]
    for i,(name,lat,lon) in enumerate(SITES):
        st=Station(id=f'DEMO.{i:02}',name=name,lat=lat,lon=lon,source='模擬')
        r=float(np.hypot(distance(lat,lon,TRUTH['lat'],TRUTH['lon']),TRUTH['depth_km']))
        t=np.arange(seconds*rate)/rate-12
        a=rng.normal(0,.015,(len(t),3))
        if not noise_only:
            for arrival, scale, axis in [(r/VP,12,0),(r/VS,45,1),(r/VS,35,2)]:
                dt=t-arrival; envelope=np.where(dt>0,(1-np.exp(-np.maximum(dt,0)*8))*np.exp(-np.maximum(dt,0)/9),0)
                a[:,axis] += scale*(35/max(r,20))**1.2*envelope*np.sin(2*np.pi*3.2*dt)
        if spike and i==0:
            a[15*rate,0]+=1500
        arrays.append((st,a))
    for second in range(seconds):
        yield [WavePacket(station=st,start=start+second,rate=rate,samples=a[second*rate:(second+1)*rate].tolist()) for st,a in arrays]
