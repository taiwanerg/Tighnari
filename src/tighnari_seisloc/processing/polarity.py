"""Non-generative AIC onset refinement and conservative first-motion diagnostics.

Bootstrap intervals measure algorithm stability, not calibrated timing accuracy.
Raw PhaseNet uncertainty is preserved; no synthetic confidence is assigned to it.
"""

import hashlib
import numpy as np
from scipy.signal import butter, sosfilt


def aic_onset(values, minimum):
    x = np.asarray(values, float)
    n = len(x)
    indices = np.arange(minimum, n - minimum)
    if not len(indices) or not np.isfinite(x).all():
        return None
    sums, squares = np.cumsum(x), np.cumsum(x * x)
    left = np.maximum(squares[indices - 1] / indices - (sums[indices - 1] / indices) ** 2, 1e-30)
    right_n = n - indices
    right = np.maximum(
        (squares[-1] - squares[indices - 1]) / right_n
        - ((sums[-1] - sums[indices - 1]) / right_n) ** 2,
        1e-30,
    )
    score = indices * np.log(left) + right_n * np.log(right)
    best = int(np.argmin(score))
    # An edge optimum is not a resolved onset.
    if best in (0, len(indices) - 1):
        return None
    return int(indices[best])


def baseline_quality(block, rate):
    """Screen the noise reference (P-3 to P-1 s) for ramps and level jumps.

    Compare changes to the within-bin scatter, independent of gain or DC offset.
    Compare a smooth excursion to the pick-window signal before rejecting it.
    """
    bins = np.array_split(block[: round(2 * rate)], 8)
    levels = np.array([np.median(chunk) for chunk in bins])
    residuals = np.concatenate([chunk - level for chunk, level in zip(bins, levels)])
    scatter = max(1.4826 * np.median(np.abs(residuals)), 1e-15)
    changes = np.diff(levels)
    excursion = levels[-1] - levels[0]
    ratio = abs(excursion) / scatter
    direction_agreement = np.mean(changes * np.sign(excursion) > 0)
    metrics = dict(excursion_to_noise=float(ratio), direction_agreement=float(direction_agreement))
    # Compare to the picker's +/-0.5 s search window. A small smooth
    # trend in a quiet baseline can coexist with a much larger clean P.
    window = block[round(2.5 * rate) : round(3.5 * rate)]
    p_range = np.quantile(window, 0.95) - np.quantile(window, 0.05)
    metrics["excursion_to_pick_range"] = float(abs(excursion) / max(p_range, 1e-15))
    if ratio >= 12 and direction_agreement >= 6 / 7 and abs(excursion) >= 0.5 * p_range:
        return "pre_P_baseline_ramp", metrics
    for split in range(2, 7):
        left, right = levels[:split], levels[split:]
        jump = abs(np.median(right) - np.median(left))
        if jump >= 12 * scatter and max(np.ptp(left), np.ptp(right)) <= jump * 0.20:
            return "pre_P_baseline_step", metrics
    # A pick can also land on the start of an instrumental ramp or jump.
    # Require a nearly straight ramp or two flat plateaux across the onset;
    # oscillating P pulses and excursions later in the record stay eligible.
    for offset in (-0.2, 0.0, 0.2):
        start = round((3 + offset) * rate)
        segment = block[start : start + round(0.6 * rate)]
        axis = np.linspace(0, 1, len(segment))
        slope, intercept = np.polyfit(axis, segment, 1)
        if abs(slope) > 20 * max(scatter, np.ptp(levels)) and np.max(
            np.abs(segment - (slope * axis + intercept))
        ) < 0.03 * abs(slope):
            return "P_window_linear_ramp", metrics
    edges = np.diff(window)
    j = int(np.argmax(np.abs(edges))) + 1
    if min(j, len(window) - j) >= round(0.12 * rate):
        left, right = window[:j], window[j:]
        jump = abs(np.median(right) - np.median(left))
        if jump > 20 * scatter and max(np.ptp(left), np.ptp(right)) < 0.05 * jump:
            return "P_window_level_step", metrics
    return None, metrics


def short_pulse(block, rate):
    """Check the earliest velocity change on two causal time scales."""
    widths = [max(2, round(seconds * rate)) for seconds in (0.025, 0.05)]
    if widths[0] == widths[1]:
        return None
    center = round(3 * rate)
    start, stop = center - round(0.30 * rate), center + round(0.30 * rate)
    times, signs, strengths = [], [], []
    for width in widths:
        average = np.convolve(block - block[0], np.ones(width) / width, mode="valid")
        # Index the difference at the last contributing sample (no forward filter).
        change = np.r_[np.zeros(2 * width - 1), average[width:] - average[:-width]]
        noise = change[round(0.3 * rate) : round(2 * rate)]
        change = change - np.median(noise)
        noise = change[round(0.3 * rate) : round(2 * rate)]
        scale = max(float(np.std(noise)), 1e-15)
        noise_peak = np.max(np.abs(noise))
        # Reject a transient already under way before the search window.
        if np.max(np.abs(change[start - width : start])) >= max(4 * scale, noise_peak):
            return None
        for factor in (4, 6):
            threshold = max(factor * scale, 1.05 * noise_peak)
            crossings = np.flatnonzero(np.abs(change[start:stop]) >= threshold)
            if not len(crossings):
                return None
            index = start + int(crossings[0])
            sign = int(np.sign(change[index]))
            pulse = sign * change[index : index + max(2, round(0.02 * rate))]
            if np.min(pulse) < 2 * scale:
                return None
            times.append(index)
            signs.append(sign)
            strengths.append(float(np.max(pulse) / scale))
    spread = float(np.ptp(times) / rate)
    if spread > 0.06 or len(set(signs)) != 1 or abs(min(times) - center) > 0.15 * rate:
        return None
    return dict(
        polarity=signs[0],
        offset_samples=min(times),
        scale_spread_s=spread,
        snr=min(strengths),
        method="causal-short-pulse-v1",
    )


def refine(record, pick, *, trials=100):
    """Return diagnostics even on rejection; never overwrite the original pick."""
    result = dict(
        id=record.id,
        original_time=pick["time"],
        original_sigma_s=pick.get("sigma_s"),
        method="AIC-multiband-noise-bootstrap-v3",
        accepted=False,
        reasons=[],
        interval_calibrated=False,
    )

    def reject(reason):
        result["reasons"].append(reason)
        return result

    if record.clipped and record.metadata.get("vertical_clipped", True):
        return reject("clipped")
    if record.metadata.get("vertical_orientation_verified") is not True:
        return reject("vertical_orientation_unverified")
    rate = record.rate
    z = np.asarray(record.velocity[:, 0], float)
    i = round((pick["time"] - record.start) * rate)
    if i < 3 * rate or i + 1.5 * rate >= len(z):
        return reject("insufficient_window")
    block = z[i - round(3 * rate) : i + round(1.5 * rate)]
    if not np.isfinite(block).all():
        return reject("gap_or_nonfinite")
    # Avoid a saturated ADC or constant segment masquerading as an onset.
    if np.ptp(block) == 0:
        return reject("constant_signal")
    problem, result["baseline_quality"] = baseline_quality(block, rate)
    if problem:
        return reject(problem)

    def try_short_pulse(reason):
        candidate = short_pulse(block, rate)
        if candidate is None:
            return reject(reason)
        offset = candidate.pop("offset_samples")
        result.update(candidate, accepted=True, primary_rejection=reason)
        result["refined_time"] = record.start + (i - round(3 * rate) + offset) / rate
        return result

    # Initialize each causal filter at the observed starting level. A constant
    # velocity offset must not become a transient in the bootstrap noise pool.
    centered = block - block[0]
    bands = []
    for low, high in [(1.0, 8.0), (2.0, 12.0)]:
        high = min(high, rate * 0.35)
        if low >= high:
            return reject("sample_rate_unsupported")
        y = sosfilt(butter(3, [low, high], fs=rate, btype="bandpass", output="sos"), centered)
        center = round(3 * rate)
        lo, hi = center - round(0.5 * rate), center + round(0.5 * rate)
        onset = aic_onset(y[lo:hi], max(3, round(0.06 * rate)))
        if onset is None:
            return reject("aic_unresolved")
        bands.append(dict(offset=lo + onset, signal=y, lo=lo, hi=hi))
    times = np.array([b["offset"] for b in bands]) / rate
    if np.ptp(times) > 0.12:
        return try_short_pulse("band_timing_disagreement")
    onset_offset = int(round(np.median(times) * rate))
    refined_time = record.start + (i - round(3 * rate) + onset_offset) / rate
    result["refined_time"] = refined_time
    if abs(refined_time - pick["time"]) > 0.4:
        return reject("refinement_shift_excessive")

    # Determine sign on unfiltered calibrated velocity, avoiding acausal filter lobes.
    baseline = np.median(block[: round(2 * rate)])
    noise = block[: round(2 * rate)] - baseline
    sigma = max(float(np.std(noise)), 1e-15)
    width = max(3, round(0.08 * rate))
    search = (
        block[max(0, onset_offset - round(0.06 * rate)) : onset_offset + round(0.15 * rate)]
        - baseline
    )
    crossings = np.flatnonzero(np.abs(search) >= 6 * sigma)
    if not len(crossings):
        return try_short_pulse("weak_departure")
    first = int(crossings[0]) + max(0, onset_offset - round(0.06 * rate))
    pulse = block[first : first + width] - baseline
    sign = int(np.sign(pulse[0]))
    if len(pulse) != width or not sign or np.mean(pulse * sign > 3 * sigma) < 0.8:
        return try_short_pulse("polarity_not_sustained")
    band_signs = [int(np.sign(np.median(b["signal"][first : first + width]))) for b in bands]
    if any(s != sign for s in band_signs):
        return reject("band_polarity_disagreement")
    seed = int.from_bytes(
        hashlib.sha256(f"{record.id}:{pick['time']:.4f}".encode()).digest()[:8], "big"
    )
    rng = np.random.default_rng(seed)
    refined, same_sign = [], 0
    for _ in range(trials):
        # Resample contiguous measured noise, preserving its short-time correlation.
        pos = int(rng.integers(0, max(1, len(noise) - width)))
        same_sign += int(np.sign(np.median(pulse + noise[pos : pos + width])) == sign)
        b = bands[int(rng.integers(len(bands)))]
        segment = b["signal"][b["lo"] : b["hi"]]
        background = b["signal"][round(0.5 * rate) : round(2 * rate)]
        pos = int(rng.integers(0, max(1, len(background) - len(segment))))
        k = aic_onset(segment + background[pos : pos + len(segment)], max(3, round(0.06 * rate)))
        if k is not None:
            refined.append((b["lo"] + k) / rate)
    if len(refined) < trials * 0.9:
        return reject("bootstrap_onset_unstable")
    lower, upper = np.quantile(refined, [0.025, 0.975])
    half_width = max(1 / rate, (upper - lower) / 2)
    result.update(
        polarity=sign,
        sigma_s=float(half_width),
        interval_95_s=[float(lower - np.median(times)), float(upper - np.median(times))],
        polarity_agreement=same_sign / trials,
        trials=trials,
        snr=float(np.max(np.abs(pulse)) / sigma),
    )
    if half_width > 0.12:
        return reject("timing_interval_too_wide")
    if same_sign / trials < 0.9:
        return reject("polarity_bootstrap_unstable")
    result["accepted"] = True
    return result
