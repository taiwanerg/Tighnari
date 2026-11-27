"""Full-waveform experimental ML, separate from the legacy initial-P estimate."""
import numpy as np
from scipy.integrate import cumulative_trapezoid
from scipy.signal import butter, sosfilt
from .location import distance


def local_magnitude(packets, event, travel_model):
    """Simulate Wood-Anderson from calibrated acceleration, use Bakun & Joyner.

    cm/s² -> m/s; ObsPy's velocity-input Wood-Anderson PAZ -> metres,
    multiplied by 1000 for the zero-to-peak amplitude in mm. The distance
    relation is the ObsPy/Bakun & Joyner (1984) baseline, not Taiwan ML.
    Only complete horizontal S windows (S-2 .. S+20 s) are accepted.
    """
    from obspy.signal.invsim import simulate_seismometer, WOODANDERSON
    rows = []
    for sid in event['used']:
        parts = sorted((p for p in packets if p.station.id == sid), key=lambda p: p.start)
        if not parts:
            continue
        st = parts[0].station
        epicentral = float(distance(event['lat'], event['lon'], st.lat, st.lon))
        r = float(np.hypot(epicentral, event['depth_km'] + st.elevation_m / 1000))
        if not 5 <= r <= 600:
            continue
        s = event['origin'] + float(travel_model.times(event['depth_km'], epicentral, 'S', st.elevation_m))
        rate = parts[0].rate
        if not np.isfinite(s) or any(p.rate != rate for p in parts):
            continue
        if any(abs(b.start - (a.start + len(a.samples)/rate)) > .5/rate for a, b in zip(parts, parts[1:])):
            continue
        begin = parts[0].start
        a = np.concatenate([np.asarray(p.samples) for p in parts])
        lo, hi = int((s-2-begin)*rate), int((s+20-begin)*rate)
        if lo < 10*rate or hi > len(a)-5*rate:
            continue
        amplitudes = []
        for axis in (1, 2):
            acc = a[:, axis] / 100
            acc -= np.median(acc[:int(5*rate)])
            vel = cumulative_trapezoid(acc, dx=1/rate, initial=0)
            vel = sosfilt(butter(2, .075, 'highpass', fs=rate, output='sos'), vel)
            wa = simulate_seismometer(vel, rate, paz_simulate=WOODANDERSON)
            amp = float(np.max(np.abs(wa[lo:hi]))) * 1000
            noise = float(np.max(np.abs(wa[int(5*rate):int(10*rate)]))) * 1000
            if amp <= max(4*noise, 1e-9):
                break
            amplitudes.append(amp)
        if len(amplitudes) != 2:
            continue
        amp = float(np.mean(amplitudes))
        ml = float(np.log10(amp) + np.log10(r/100) + .00301*(r-100) + 3)
        rows.append(dict(id=sid, magnitude=ml, amplitude_mm=amp, distance_km=r))
    return dict(magnitude=float(np.median([r['magnitude'] for r in rows])) if len(rows)>=3 else None,
                magnitude_count=len(rows), magnitude_type='ML (experimental)',
                magnitude_method='Wood-Anderson / Bakun & Joyner 1984; uncalibrated for Taiwan',
                magnitude_stations=rows, magnitude_saturation=False)
