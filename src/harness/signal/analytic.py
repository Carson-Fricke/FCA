"""Analytic-signal kernels for band-limited series.

Given a real band-limited detail series (for example a MODWT MRA detail),
the analytic signal ``z = x + i * Hilbert(x)`` yields the instantaneous
envelope ``|z|`` and phase ``arg(z)``. This is the practical route to
complex-wavelet features without a complex filter bank: the envelope
measures oscillation amplitude and the phase separates trending from
mean-reverting behavior at the series' scale.

The Hilbert transform is computed with the FFT and is circular, so phase
and envelope near the series edges inherit the same boundary caveat as the
underlying wavelet coefficients; callers should mask boundary samples
before aggregating.
"""

from __future__ import annotations

import numpy as np


def _hilbert_transform(series: np.ndarray) -> np.ndarray:
    """Return the analytic signal of a real 1-d series."""
    count = series.size
    spectrum = np.fft.fft(series)
    weights = np.zeros(count, dtype=np.float64)
    if count % 2 == 0:
        weights[0] = 1.0
        weights[count // 2] = 1.0
        weights[1 : count // 2] = 2.0
    else:
        weights[0] = 1.0
        weights[1 : (count + 1) // 2] = 2.0
    return np.fft.ifft(spectrum * weights)


def analytic_signal(values: object) -> tuple[np.ndarray, np.ndarray]:
    """Return the (envelope, phase) of a real 1-d series.

    The envelope is ``|z|`` and the phase is ``arg(z)`` in radians on
    ``(-pi, pi]``.
    """
    series = np.asarray(values, dtype=np.float64)
    if series.ndim != 1:
        raise ValueError("analytic input must be one-dimensional")
    if not np.all(np.isfinite(series)):
        raise ValueError("analytic input must be finite")
    analytic = _hilbert_transform(series)
    return np.abs(analytic), np.angle(analytic)


def instantaneous_phase_velocity(phase: np.ndarray) -> np.ndarray:
    """Per-sample phase velocity from an unwrapped phase series.

    Returns the cyclic difference of the unwrapped phase in radians per
    sample; element zero is set to the first finite velocity's value.
    """
    unwrapped = np.unwrap(np.asarray(phase, dtype=np.float64))
    velocity = np.empty_like(unwrapped)
    if unwrapped.size == 1:
        velocity[0] = 0.0
        return velocity
    velocity[1:] = np.diff(unwrapped)
    velocity[0] = velocity[1]
    return velocity


def phase_coherence(phase: np.ndarray) -> float:
    """Mean resultant length of successive phase increments.

    Measures the steadiness of the cycle rate: 1.0 indicates a
    monochromatic phase advance (constant increment), values near 0.0
    indicate dephased or noise-like behavior. Computed on the cyclic
    differences of the unwrapped phase over finite samples.
    """
    values = np.asarray(phase, dtype=np.float64)
    finite = values[np.isfinite(values)]
    if finite.size < 2:
        return float("nan")
    increments = np.diff(np.unwrap(finite))
    return float(np.abs(np.mean(np.exp(1j * increments))))


def block_phase_features(
    phase: np.ndarray, blocks: list[tuple[int, int]]
) -> tuple[np.ndarray, np.ndarray]:
    """Per-block mean phase velocity and coherence.

    ``blocks`` holds ``(start, stop)`` half-open index ranges. Blocks with
    fewer than two finite samples return ``nan`` velocity and coherence.
    """
    velocity = instantaneous_phase_velocity(phase)
    speeds = np.empty(len(blocks), dtype=np.float64)
    coherences = np.empty(len(blocks), dtype=np.float64)
    for index, (start, stop) in enumerate(blocks):
        start = max(start, 0)
        stop = min(stop, phase.size)
        if stop - start < 2:
            speeds[index] = np.nan
            coherences[index] = np.nan
            continue
        samples = velocity[start:stop]
        finite = samples[np.isfinite(samples)]
        if finite.size == 0:
            speeds[index] = np.nan
            coherences[index] = np.nan
            continue
        speeds[index] = float(finite.mean())
        coherences[index] = phase_coherence(phase[start:stop])
    return speeds, coherences
