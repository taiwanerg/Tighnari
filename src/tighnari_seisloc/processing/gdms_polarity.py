"""Bounded GDMS P repicking and timing-marginalized first-motion evidence."""

import numpy as np
from scipy.signal import butter, sosfilt
from scipy.special import ndtr

from .polarity import baseline_quality


def measure(record, center, half_width, *, latest=None):
    """Return a new timing candidate and polarity evidence, with no pick mutation.

    Equal weight is placed on the available band/threshold timing hypotheses.
    The conditional model includes an unresolved class for small changes.
    These probabilities are conditional on the waveform model, not calibrated.
    """
    result = dict(
        method="gdms-timing-marginalized-v1",
        accepted=False,
        timing_accepted=False,
        reasons=[],
        search_center=center,
        search_half_width_s=half_width,
    )

    def reject(reason):
        result["reasons"].append(reason)
        return result

    rate = record.rate
    if not np.isfinite(rate) or rate < 20:
        return reject("sample_rate_unsupported")
    start = round((center - half_width - 4 - record.start) * rate)
    stop = round((center + half_width + 1 - record.start) * rate)
    if start < 0 or stop > len(record.velocity):
        return reject("insufficient_window")
    block = np.asarray(record.velocity[start:stop, 0], float)
    if not np.isfinite(block).all():
        return reject("gap_or_nonfinite")
    base_time = record.start + start / rate
    lo = round((center - half_width - base_time) * rate)
    hi = round((center + half_width - base_time) * rate)
    if latest is not None:
        hi = min(hi, int(np.floor((latest - base_time) * rate)))
    if hi <= lo:
        return reject("no_p_before_s")
    noise_start, noise_end = round(rate), lo - round(0.25 * rate)
    envelope_width = max(2, round(0.06 * rate))
    by_band = []
    for low, high in ((1, 8), (2, 12)):
        band = sosfilt(
            butter(3, (low, min(high, 0.4 * rate)), btype="bandpass", fs=rate, output="sos"),
            block - block[0],
        )
        power = np.convolve(band**2, np.ones(envelope_width) / envelope_width)
        envelope = np.sqrt(power[: len(block)])
        noise = envelope[noise_start:noise_end]
        early = envelope[lo - round(0.2 * rate) : lo]
        band_candidates = []
        for factor in (4, 6):
            threshold = max(factor * np.median(noise), 1.2 * np.max(noise), 1e-15)
            crossings = np.flatnonzero(envelope[lo:hi] >= threshold)
            if len(crossings) and np.max(early) < threshold:
                index = lo + int(crossings[0])
                # A filter detection is delayed. Explore the preceding rising edge.
                edge = max(lo, index - round(0.14 * rate))
                below = np.flatnonzero(envelope[edge:index] <= 0.5 * threshold)
                onset = edge + int(below[-1]) + 1 if len(below) else edge
                band_candidates.append(onset)
        by_band.append(band_candidates)
    triggers = [len(indices) for indices in by_band]
    candidates = [index for indices in by_band for index in indices]
    result["band_trigger_counts"] = triggers
    result["candidate_times"] = [base_time + index / rate for index in candidates]
    if min(triggers) == 0:
        return reject("no_coincident_p_onset")
    if np.ptp(candidates) / rate > 0.18:
        return reject("repick_timing_disagreement")

    pick_index = int(round(np.median(candidates)))
    result["refined_time"] = base_time + pick_index / rate
    before = start + pick_index - round(3 * rate)
    after = start + pick_index + round(1.5 * rate)
    if before < 0 or after > len(record.velocity):
        return reject("insufficient_window")
    quality = record.velocity[before:after, 0]
    if not np.isfinite(quality).all():
        return reject("gap_or_nonfinite")
    problem, result["baseline_quality"] = baseline_quality(quality, rate)
    if problem:
        return reject(problem)

    result["timing_accepted"] = True
    probabilities, strengths = [], []
    # Signed change from pre-onset velocity to the first short post-onset window.
    # Use the same contrast on pre-P noise to estimate its uncertainty.
    for seconds in (0.04, 0.07):
        width = max(1, round(seconds * rate))
        mean = np.convolve(block - block[0], np.ones(width) / width, mode="valid")
        change = mean[width:] - mean[:-width]
        noise_change = change[noise_start : noise_end - 2 * width]
        bias = np.median(noise_change)
        noise_change = noise_change - bias
        scale = max(float(1.4826 * np.median(np.abs(noise_change))), 1e-15)
        threshold = max(3 * scale, float(np.max(np.abs(noise_change))))
        shifts = (-round(0.025 * rate), 0, round(0.025 * rate))
        for band_candidates in by_band:
            offsets = np.array(band_candidates)[:, None] + shifts
            delta = change[offsets - width] - bias
            probabilities.append(
                [
                    np.mean(ndtr((delta - threshold) / scale)),
                    np.mean(ndtr((-delta - threshold) / scale)),
                ]
            )
            strengths.extend((np.abs(delta) / scale).ravel())
    up, down = map(float, np.mean(probabilities, axis=0))
    result["polarity_probabilities"] = dict(up=up, down=down, unresolved=max(0.0, 1 - up - down))
    result["snr"] = float(np.median(strengths))
    if max(up, down) < 0.90:
        return reject("polarity_probability_low")
    result.update(accepted=True, polarity=1 if up > down else -1)
    return result
