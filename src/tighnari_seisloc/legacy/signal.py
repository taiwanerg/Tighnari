"""Causal, bounded-window signal features; thresholds are research defaults."""
import numpy as np
from scipy.signal import butter, sosfilt
from scipy.integrate import cumulative_trapezoid

LEVELS = ['0', '1', '2', '3', '4', '5弱', '5強', '6弱', '6強', '7']


def intensity(pga, pgv=None):
    if pga < 80:
        return LEVELS[int(np.searchsorted([.8, 2.5, 8, 25], pga, side='right'))]
    if pgv is None:
        return None  # Strong-motion grade requires velocity, not PGA alone.
    return LEVELS[max(4, 5 + int(np.searchsorted([30, 50, 80, 140], pgv, side='right')))] if pgv >= 15 else '4'


def features(samples, rate):
    a = np.asarray(samples, dtype=float)
    # Initial baseline only, preserving causality when later packets arrive.
    a = a - np.median(a[:min(len(a), int(rate * 2))], axis=0)
    filtered = sosfilt(butter(4, 10, fs=rate, output='sos'), a, axis=0) if rate > 20 else a
    pga = float(np.max(np.linalg.norm(filtered, axis=1)))
    v = cumulative_trapezoid(a, dx=1/rate, axis=0, initial=0)
    v = sosfilt(butter(2, .075, 'highpass', fs=rate, output='sos'), v, axis=0)
    pgv = float(np.max(np.linalg.norm(v, axis=1)))
    return pga, pgv


def pick_p(samples, rate, since=0):
    """STA/LTA candidate + sustained energy/SNR + impulse rejection + AIC repick.

    Needs 8s clean warmup. Returns an onset offset, never a receipt timestamp.
    Filters use past data only; the confirmation window adds up to .8s latency.
    """
    a = np.asarray(samples, dtype=float)
    nsta, nlta = int(.4*rate), int(8*rate)
    if len(a) < nlta + int(rate):
        return None
    z = a[:, 0] - np.median(a[:int(2*rate), 0])
    z = sosfilt(butter(3, [.8, min(8, rate*.4)], 'bandpass', fs=rate, output='sos'), z)
    energy = z*z
    cs = np.r_[0., np.cumsum(energy)]
    sta = (cs[nsta:] - cs[:-nsta])/nsta
    # LTA ends before STA: earthquake energy does not immediately lift the baseline.
    for i in range(max(nlta+nsta, int(since*rate)), len(z)-int(.8*rate), max(1, int(.05*rate))):
        base = energy[i-nsta-nlta:i-nsta]
        lta = float(np.mean(base)) + 1e-12
        if sta[i-nsta] / lta < 6:
            continue
        before = z[max(0, i-int(2*rate)):i-int(.5*rate)]
        after = z[i:i+int(.8*rate)]
        noise = max(float(np.sqrt(np.mean(before*before))), .005)
        if np.sqrt(np.mean(after*after))/noise < 4:
            continue
        # A mechanical spike occupies few raw samples; do not let filter ringing pass.
        raw = a[max(0, i-int(.5*rate)):i+int(.8*rate), 0]
        raw = raw - np.median(a[:int(2*rate), 0])
        if np.sum(raw*raw) <= 0 or np.max(raw*raw)/np.sum(raw*raw) > .3:
            continue
        if np.count_nonzero(np.abs(after) > noise*3) < int(.2*rate):
            continue
        lo, hi = max(nlta, i-int(1.2*rate)), min(len(z), i+int(.4*rate))
        window = z[lo:hi]
        ks = np.arange(max(3, int(.1*rate)), len(window)-max(3, int(.1*rate)))
        if not len(ks):
            continue
        aic = [k*np.log(np.var(window[:k])+1e-15) + (len(window)-k)*np.log(np.var(window[k:])+1e-15) for k in ks]
        return (lo+int(ks[int(np.argmin(aic))]))/rate
    return None


def initial_pd(samples, rate, onset):
    """Vertical 3-s peak displacement in cm, with causal 0.075-Hz drift filter.

    Require the entire window and a displacement-domain SNR of at least four.
    Numerical integration approximates the velocity-record workflow of Wu et al.
    (2006); it still requires sensor/network validation for acceleration inputs.
    """
    i=int(round(onset*rate)); n=int(3*rate)
    a=np.asarray(samples,dtype=float)[:,0]
    if i<n or i+n>len(a): return None
    a=a-np.median(a[:min(int(2*rate),i)])
    v=cumulative_trapezoid(a,dx=1/rate,initial=0)
    v=sosfilt(butter(2,.075,'highpass',fs=rate,output='sos'),v)
    d=cumulative_trapezoid(v,dx=1/rate,initial=0)
    d=sosfilt(butter(2,.075,'highpass',fs=rate,output='sos'),d)
    pd=float(np.max(np.abs(d[i:i+n])))
    noise=float(np.max(np.abs(d[i-n:i])))
    return pd if pd>max(noise*4,1e-5) else None
