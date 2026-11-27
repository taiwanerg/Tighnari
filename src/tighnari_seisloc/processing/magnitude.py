"""Taiwan Wood–Anderson ML; independent long-period Tsuboi Mwp proxy.

Shin (1993) and Guan et al. (2020), equations 2–3, with the Tighnari WA gain.
No station correction is learned from the five regression events.
"""

from copy import deepcopy
import math
import numpy as np
from scipy.signal import butter, sosfiltfilt, find_peaks
from scipy.integrate import cumulative_trapezoid
from ..legacy.location import distance

WOOD_ANDERSON_GAIN = 6800


def shin_log_a0(epicentral_km, depth_km):
    r = max(1, float(np.hypot(epicentral_km, depth_km)))
    if depth_km > 35:
        return -0.00326 * r - 0.83 * np.log10(r) - 1.01
    if epicentral_km <= 80:
        return -0.00716 * r - np.log10(r) - 0.39
    return -0.00261 * r - 0.83 * np.log10(r) - 1.07


def taiwan_ml(records, event, model, corrections=None):
    from obspy.signal.invsim import WOODANDERSON, simulate_seismometer

    corrections = corrections or {}
    paz = deepcopy(WOODANDERSON)
    paz["sensitivity"] = WOOD_ANDERSON_GAIN
    rows = []
    candidates = []
    for record in records:
        row = dict(id=record.id, accepted=False, reason=None)
        delta = float(distance(event["lat"], event["lon"], record.lat, record.lon))
        row["distance_km"] = delta
        if not 0 <= delta <= 600:
            row["reason"] = "incomplete-S-window-or-outside-domain"
            rows.append(row)
            continue
        arrival = event["origin"] + float(
            model.times(event["depth_km"], delta, "S", record.elevation_m)
        )
        if not np.isfinite(arrival):
            row["reason"] = "incomplete-S-window-or-outside-domain"
            rows.append(row)
            continue
        lo = int((arrival - 2 - record.start) * record.rate)
        # Window can include later S coda but is bounded by the archived segment.
        hi = int((arrival + 30 - record.start) * record.rate)
        if (
            not np.isfinite(arrival)
            or not 0 <= delta <= 600
            or lo < 10 * record.rate
            or hi > len(record.velocity) - 5 * record.rate
        ):
            row["reason"] = "incomplete-S-window-or-outside-domain"
        elif record.clipped:
            row["reason"] = "clipped"
        elif not np.isfinite(record.velocity[:, 1:]).all():
            row["reason"] = "missing-horizontal-component"
        else:
            amps = []
            snrs = []
            for axis in (1, 2):
                wa = simulate_seismometer(record.velocity[:, axis], record.rate, paz_simulate=paz)
                amp = float(np.max(np.abs(wa[lo:hi]))) * 1000
                # Noise is measured immediately before predicted P, not at an arbitrary file edge.
                p = event["origin"] + float(model.times(event["depth_km"], delta, "P"))
                ni = int((p - record.start - 3) * record.rate)
                noise = (
                    float(
                        np.sqrt(np.mean(wa[max(0, ni - int(20 * record.rate)) : max(1, ni)] ** 2))
                    )
                    * 1000
                )
                snrs.append(amp / max(noise, 1e-12))
                amps.append(amp)
            if min(snrs) < 4 or min(amps) <= 0:
                row["reason"] = "low-horizontal-SNR"
            else:
                amplitude = float(np.hypot(*amps))
                value = float(
                    np.log10(amplitude)
                    - shin_log_a0(delta, event["depth_km"])
                    + corrections.get(record.id, 0)
                )
                row.update(
                    accepted=True,
                    amplitude_mm=amplitude,
                    magnitude=value,
                    snr=min(snrs),
                    station_correction=corrections.get(record.id, 0),
                )
                candidates.append((record, row))
        rows.append(row)
    # Evaluate alternatives before choosing one independent site. A missing S
    # window or clipped primary must not suppress a usable co-located sensor.
    candidates.sort(
        key=lambda item: (
            {"HH": 4, "BH": 3, "EH": 2, "HN": 1}.get(item[0].family, 0),
            item[1]["snr"],
        ),
        reverse=True,
    )
    seen = []
    for record, row in candidates:
        if any(distance(record.lat, record.lon, other.lat, other.lon) < 2 for other in seen):
            row.update(accepted=False, reason="co-located-alternative")
        else:
            seen.append(record)
    # Surface S-13 short-period sites read ~0.8 ML above the borehole HH and
    # vault BH sites this WA gain is calibrated against (and above CWA ML), and
    # no station corrections exist for them yet. Use them only to reach three sites.
    short_period = [row for record, row in candidates if row["accepted"] and record.family == "EH"]
    broadband = sum(
        1 for record, row in candidates if row["accepted"] and record.family in ("HH", "BH")
    )
    short_period_policy = None
    if short_period and broadband >= 3:
        for row in short_period:
            row.update(accepted=False, reason="surface-short-period-excluded")
        short_period_policy = "excluded"
    elif short_period:
        short_period_policy = "fallback"
    accepted = [r for r in rows if r["accepted"]]
    if len(accepted) >= 4:
        median = np.median([r["magnitude"] for r in accepted])
        mad = np.median([abs(r["magnitude"] - median) for r in accepted])
        for r in accepted:
            if abs(r["magnitude"] - median) > max(0.7, 3 * 1.4826 * mad):
                r.update(accepted=False, reason="network-magnitude-outlier")
    accepted = [r for r in rows if r["accepted"]]
    values = [r["magnitude"] for r in accepted]
    return dict(
        type="ML",
        value=float(np.median(values)) if len(values) >= 3 else None,
        count=len(values),
        method=f"Shin1993-Taiwan/WA{WOOD_ANDERSON_GAIN}-horizontal-RSS",
        station_correction_status="uncalibrated-zero" if not corrections else "external-file",
        short_period_policy=short_period_policy,
        saturation_risk=bool(values and np.median(values) >= 6.5),
        uncertainty=float(np.std(values)) if values else None,
        stations=rows,
        reason=None if len(values) >= 3 else "fewer-than-three-quality-sites",
    )


def mwp_from_integral(integral_m_s, distance_deg):
    """SI units; radiation coefficient applied once; no empirical ML conversion."""
    if integral_m_s <= 0 or not 30 <= distance_deg <= 90:
        raise ValueError("Mwp requires positive m*s amplitude and 30–90 degrees")
    moment = (
        4 * math.pi * 3400 * 7900**3 * (6371000 * math.radians(distance_deg)) * integral_m_s / 0.52
    )
    return (math.log10(moment) - 9.1) / 1.5


def measure_mwp(
    velocity, rate, start, p_arrival, s_arrival, distance_deg, *, quality_velocity=None
):
    """Research filtered integration, not a reproduction of operational PTWC.

    Separate low-frequency path; .008 Hz high-pass before/after integration.
    Baseline P−360..P−60 follows the physical-method reference.
    """
    if not 30 <= distance_deg <= 90:
        return dict(accepted=False, reason="distance-outside-30-90-deg")
    i = int((p_arrival - start) * rate)
    n0, n1 = int(i - 360 * rate), int(i - 60 * rate)
    end = int(min(i + 120 * rate, (s_arrival - start) * rate))
    if n0 < 0 or end > len(velocity) or end <= i + 10 * rate:
        return dict(accepted=False, reason="incomplete-baseline-or-P-window")
    vel = np.asarray(velocity, float) - np.mean(velocity[n0:n1])
    quality = vel if quality_velocity is None else np.asarray(quality_velocity, float)
    if quality.shape != vel.shape or not np.isfinite(quality).all():
        return dict(accepted=False, reason="invalid-broadband-quality-trace")
    filtered = sosfiltfilt(
        butter(2, [0.3, min(5, rate * 0.4)], btype="bandpass", fs=rate, output="sos"), quality
    )
    noise = np.sqrt(np.mean(filtered[i - int(60 * rate) : i] ** 2))
    snr = float(np.sqrt(np.mean(filtered[i : i + int(60 * rate)] ** 2)) / max(noise, 1e-15))
    if snr < 5:
        return dict(accepted=False, reason="P-SNR-below-five", snr=snr)
    hp = butter(2, 0.008, fs=rate, btype="highpass", output="sos")
    v = sosfiltfilt(hp, vel)
    disp = cumulative_trapezoid(v, dx=1 / rate, initial=0)
    integral = cumulative_trapezoid(disp, dx=1 / rate, initial=0)
    integral = sosfiltfilt(hp, integral)
    window = integral[i:end] - integral[i]
    peaks, _ = find_peaks(np.abs(window), prominence=max(np.max(np.abs(window)) * 0.1, 1e-15))
    if not len(peaks):
        return dict(accepted=False, reason="monotonic-or-unresolved-integral", snr=snr)
    amplitude = float(np.max(np.abs(window[peaks])))
    return dict(
        accepted=True,
        magnitude=mwp_from_integral(amplitude, distance_deg),
        integral_m_s=amplitude,
        snr=snr,
        distance_deg=distance_deg,
    )


def aggregate_mwp(results, *, method="Tsuboi-physical-SI/HP0.008Hz", **metadata):
    """Identical independent-site and robust-outlier gates for live and archive."""
    rows = [dict(row) for row in results]
    seen = set()
    coordinates = []
    for row in sorted(rows, key=lambda r: r.get("snr", 0), reverse=True):
        if not row.get("accepted"):
            continue
        if not np.isfinite(row.get("magnitude", np.nan)):
            row.update(accepted=False, reason="nonfinite-magnitude")
            continue
        site = (row.get("network"), row.get("station"))
        if site == (None, None):
            site = tuple(row.get("id", "").split(".")[:2])
        if len(site) != 2 or not all(site):
            row.update(accepted=False, reason="station-identity-unavailable")
        elif site in seen or (
            all(k in row for k in ("lat", "lon"))
            and any(distance(row["lat"], row["lon"], lat, lon) < 2 for lat, lon in coordinates)
        ):
            row.update(accepted=False, reason="co-located-alternative")
        else:
            seen.add(site)
            if all(k in row for k in ("lat", "lon")):
                coordinates.append((row["lat"], row["lon"]))
    good = [r["magnitude"] for r in rows if r.get("accepted")]
    if len(good) >= 5:
        median = float(np.median(good))
        mad = float(np.median(np.abs(np.asarray(good) - median)))
        for row in rows:
            if row.get("accepted") and abs(row["magnitude"] - median) > max(0.6, 3 * 1.4826 * mad):
                row.update(accepted=False, reason="network-magnitude-outlier")
    good = [r["magnitude"] for r in rows if r.get("accepted")]
    return dict(
        **metadata,
        type="Mwp",
        value=float(np.median(good)) if len(good) >= 5 else None,
        count=len(good),
        uncertainty=float(np.std(good)) if good else None,
        stations=rows,
        method=method,
        reason=None if len(good) >= 5 else "fewer-than-five-quality-far-stations",
    )
