"""Robust P-arrival inversion with homogeneous or external tabulated travel times."""
import numpy as np
from scipy.optimize import least_squares

VP, VS = 6.0, 3.5


def distance(lat, lon, lat2, lon2):
    a, b = np.radians(lat), np.radians(lat2)
    h = np.sin((b-a)/2)**2 + np.cos(a)*np.cos(b)*np.sin(np.radians(np.asarray(lon2)-lon)/2)**2
    return 6371*2*np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def locate(picks, travel_model=None):
    if len(picks) < 4:
        return None
    from .travel import HomogeneousTravelTime
    model=travel_model or HomogeneousTravelTime()
    lats = np.array([p['lat'] for p in picks]); lons = np.array([p['lon'] for p in picks])
    elev = np.array([p.get('elevation_m', 0)/1000 for p in picks])
    times = np.array([p['time'] for p in picks]); anchor = times.min(); t = times-anchor
    def residual(x):
        theory=model.times(x[2], distance(x[0], x[1], lats, lons), elevation_m=elev*1000)
        return np.where(np.isfinite(theory), x[3]+theory-t, 1e4)
    bounds=([20,117,1,-180],[27,125,model.max_depth,1])
    seeds=[]
    if travel_model is not None:
        # Coarse global search marginalizes origin by median. Vectorized over
        # trial cells and stations; avoids a local minimum at the station centroid.
        glat=np.arange(max(20,lats.min()-.6),min(27,lats.max()+.6)+.05,.2)
        glon=np.arange(max(117,lons.min()-.6),min(125,lons.max()+.6)+.05,.2)
        gz=np.array([4,10,20,35,60,100,180,250],float)
        gz=gz[gz<=model.max_depth]
        mesh=np.array(np.meshgrid(glat,glon,gz,indexing='ij')).reshape(3,-1).T
        dist=distance(mesh[:,0,None],mesh[:,1,None],lats[None,:],lons[None,:])
        pred=model.times(mesh[:,2,None],dist,elevation_m=elev[None,:]*1000)
        origin=np.median(t[None,:]-pred,axis=1)
        r=origin[:,None]+pred-t[None,:]
        # Minimize a trimmed residual score, preserving at least four observations.
        keep=max(4,len(picks)-max(1,len(picks)//5))
        score=np.mean(np.sort(r*r,axis=1)[:,:keep],axis=1)
        for idx in np.argsort(np.nan_to_num(score,nan=np.inf))[:50]:
            trial=mesh[idx]
            if not np.isfinite(score[idx]) or not -180<=origin[idx]<=1: continue
            if all(np.linalg.norm((trial-x[:3])*[111,100,1])>15 for x in seeds):
                seeds.append(np.r_[trial,origin[idx]])
            if len(seeds)>=6:break
    else:
        for depth in (8,25,60):
            seeds.append([lats.mean(),lons.mean(),depth,-depth/VP])
    fits=[]
    for seed in seeds:
        fit=least_squares(residual,seed,bounds=bounds,loss='soft_l1',f_scale=.35)
        if np.max(np.abs(residual(fit.x)))<1e4:fits.append(fit)
    if not fits:return None
    fit = min(fits, key=lambda f: np.sum(np.minimum(residual(f.x)**2, 4)))
    mask = np.abs(residual(fit.x)) < 1.0
    if mask.sum() < 4:
        return None
    if not mask.all():
        fit = least_squares(lambda x: residual(x)[mask], fit.x,
                            bounds=bounds, loss='soft_l1', f_scale=.35)
    r = residual(fit.x)[mask]; lat, lon, depth, origin = fit.x
    az = np.sort(np.degrees(np.arctan2((lons[mask]-lon)*np.cos(np.radians(lat)), lats[mask]-lat)) % 360)
    gap = float(np.max(np.diff(np.r_[az, az[0]+360])))
    # Approximate local covariance, explicitly not a confidence bound for model errors.
    jac = fit.jac
    cov = np.linalg.pinv(jac.T@jac)*max(float(np.mean(r*r)), .04)
    uncertainty = float(np.sqrt(max(cov[0,0],0)+max(cov[1,1],0))*111)
    return dict(lat=float(lat), lon=float(lon), depth_km=float(depth), origin=anchor+float(origin),
                rms_s=float(np.sqrt(np.mean(r*r))), gap_deg=gap, horizontal_sigma_km=uncertainty,
                depth_sigma_km=float(np.sqrt(max(cov[2,2],0))),degrees_of_freedom=max(0,int(mask.sum())-4),
                travel_model=model.name,
                residuals=[dict(id=p['id'],observed=p['time'],predicted=anchor+float(fit.x[3])+float(model.times(depth,distance(lat,lon,p['lat'],p['lon']),elevation_m=p.get('elevation_m',0))),residual_s=float(residual(fit.x)[i]),used=bool(mask[i])) for i,p in enumerate(picks)],
                used=[p['id'] for p, good in zip(picks, mask) if good], count=int(mask.sum()))


def estimate_magnitude(stations, event):
    """Taiwan NACB Pd relation, Wu et al. 2006, eq. 6 (cm, km).

    Restrict to the study's shallow/local domain. Never silently substitute PGA.
    """
    values=[]
    if event['depth_km'] > 30: return None, 0
    for st in stations:
        pd=st.get('pd_cm')
        if st['id'] not in event['used'] or not pd: continue
        epicentral=float(distance(event['lat'],event['lon'],st['lat'],st['lon']))
        if epicentral > 100: continue
        r=max(1.,float(np.hypot(epicentral,event['depth_km']+st.get('elevation_m',0)/1000)))
        values.append(5.265+1.385*np.log10(pd)+2.000*np.log10(r))
    if len(values)<3: return None,len(values)
    return float(np.clip(np.median(values),0,9)),len(values)


def predict_pga(m, r):
    r = max(5., float(r))
    return float(10**(.5*m+1.3-np.log10(r)-.003*r))
