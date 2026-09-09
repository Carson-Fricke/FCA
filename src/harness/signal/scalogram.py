"""Morlet continuous wavelet transform for exploratory scalograms.

Computes the complex CWT with the analytic Morlet mother wavelet

    psi_a(t) = a**-0.5 * pi**-0.25 * exp(1j * omega0 * t / a)
               * exp(-0.5 * (t / a)**2)

via zero-padded FFT correlation. The CWT is a redundant, non-orthogonal
transform: its coefficients cannot reconstruct the series and must not be
used as model features. Its role here is the exploratory time-frequency
artifact (per-cluster mean power scalograms) that visualizes what the
MODWT feature scales are picking up.

With ``omega0 = 6`` the center period of scale ``a`` is approximately
``(4 * pi / (omega0 + sqrt(2 + omega0**2))) * a`` for unit sample spacing.
"""

from __future__ import annotations

import numpy as np

DEFAULT_OMEGA0 = 6.0
_CONE_CONSTANT = 4.0 * np.pi / (DEFAULT_OMEGA0 + np.sqrt(2.0 + DEFAULT_OMEGA0**2))


def morlet_periods(scales: np.ndarray) -> np.ndarray:
    """Center period of each Morlet scale for unit sample spacing."""
    return _CONE_CONSTANT * np.asarray(scales, dtype=np.float64)


def log_scales(
    min_period: float, max_period: float, voices_per_octave: int = 12
) -> np.ndarray:
    """Log-spaced Morlet scales covering ``[min_period, max_period]``.

    ``min_period`` and ``max_period`` are periods in samples; the returned
    scales are chosen so Morlet center periods hit the requested range.
    """
    if min_period <= 0.0 or max_period <= min_period:
        raise ValueError(
            f"need 0 < min_period < max_period, got {min_period}, {max_period}"
        )
    if voices_per_octave < 1:
        raise ValueError("voices_per_octave must be >= 1")
    base = _CONE_CONSTANT
    low = np.log2(min_period / base)
    high = np.log2(max_period / base)
    count = int(np.floor((high - low) * voices_per_octave)) + 1
    exponents = low + np.arange(count) / voices_per_octave
    return np.power(2.0, exponents)


def _cone_mask(width: int, scales: np.ndarray, edge: float = 2.0) -> np.ndarray:
    """Boolean mask of samples outside the cone of influence per scale."""
    periods = morlet_periods(scales)
    half = edge * periods / 2.0
    index = np.arange(width, dtype=np.float64)
    return (index[None, :] >= half[:, None]) & (
        index[None, :] <= (width - 1) - half[:, None]
    )


def morlet_cwt(
    values: object,
    scales: np.ndarray,
    *,
    omega0: float = DEFAULT_OMEGA0,
) -> np.ndarray:
    """Complex Morlet CWT of a real 1-d series.

    Returns an array of shape ``(len(scales), len(series))`` holding
    ``W(a, b) = sum_t x[t] * conj(psi_a(t - b))`` computed with zero-padded
    FFT correlation (linear, wrap-free).
    """
    series = np.asarray(values, dtype=np.float64)
    scales = np.asarray(scales, dtype=np.float64)
    if series.ndim != 1:
        raise ValueError("CWT input must be one-dimensional")
    if not np.all(np.isfinite(series)):
        raise ValueError("CWT input must be finite")
    if scales.ndim != 1 or scales.size == 0 or np.any(scales <= 0.0):
        raise ValueError("scales must be a non-empty positive 1-d array")
    count = series.size
    coefficients = np.empty((scales.size, count), dtype=np.complex128)
    for index, scale in enumerate(scales):
        reach = int(np.ceil(omega0 * scale))
        grid = np.arange(-reach, reach + 1, dtype=np.float64)
        mother = (
            np.exp(1j * omega0 * grid / scale)
            * np.exp(-0.5 * np.square(grid / scale))
            / (np.sqrt(scale) * np.pi**0.25)
        )
        reversed_kernel = np.conj(mother[::-1])
        size = 1
        while size < count + reversed_kernel.size - 1:
            size *= 2
        embedded = np.zeros(size, dtype=np.complex128)
        embedded[: reversed_kernel.size] = reversed_kernel
        convolved = np.fft.ifft(np.fft.fft(series, size) * np.fft.fft(embedded))
        start = reversed_kernel.size - 1
        coefficients[index] = convolved[start : start + count]
    return coefficients


def scalogram(
    values: object,
    scales: np.ndarray,
    *,
    omega0: float = DEFAULT_OMEGA0,
) -> tuple[np.ndarray, np.ndarray]:
    """Wavelet power scalogram and its cone-of-influence mask.

    Returns ``(power, mask)`` where ``power`` has shape
    ``(len(scales), len(series))`` and ``mask`` marks samples free of edge
    contamination at each scale (True = usable).
    """
    series = np.asarray(values, dtype=np.float64)
    coefficients = morlet_cwt(series, scales, omega0=omega0)
    power = np.abs(coefficients) ** 2
    return power, _cone_mask(series.size, np.asarray(scales, dtype=np.float64))
