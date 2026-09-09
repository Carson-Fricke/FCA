"""Maximal-overlap discrete wavelet transform kernels.

Implements the circular MODWT pyramid (Percival & Walden, *Wavelet Methods
for Time Series Analysis*) with least-asymmetric and Daubechies orthonormal
filter banks, the additive multiresolution analysis (MRA), the exact inverse
transform, and unbiased wavelet-variance estimates.

Boundary handling:

- ``boundary="circular"`` treats the signal as one period; the transform is
  then exactly invertible, energy-preserving, and shift-invariant on the
  given samples.
- ``boundary="reflection"`` (default) reflect-pads the signal by the widest
  level filter and crops back, which keeps clock-time alignment at the cost
  of extension semantics at the edges. With the causal analysis filters
  used here, level-``j`` wavelet coefficients read the extension only in
  their first ``L_j - 1`` samples, where
  ``L_j = (2**j - 1) * (L - 1) + 1`` is the level-``j`` filter width and
  ``L`` the base filter length; the trailing samples are boundary-free.
  The unbiased wavelet-variance estimator excludes those leading samples
  (WMTSA convention, ``M_j = N - L_j + 1``). The MRA built by
  :func:`modwt_mra` synthesizes on the padded grid before cropping, so
  ``sum(details) + smooth`` reproduces the input exactly at every sample
  and only the *interpretation* of edge samples (they see mirrored data)
  carries a boundary caveat.

The transform works on any series length; there is no power-of-two
restriction. All inputs must be finite float64-convertible arrays.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_SQRT2 = float(np.sqrt(2.0))

LA8_LOWPASS: tuple[float, ...] = (
    -0.07576571478927333,
    -0.02963552764599851,
    0.49761866763201545,
    0.8037387518059161,
    0.29785779560527736,
    -0.09921954357684722,
    -0.012603967262037833,
    0.03222310060404270,
)
DB4_LOWPASS: tuple[float, ...] = (
    0.23037781330885523,
    0.7148465705525415,
    0.6308807679295904,
    -0.02798376941698385,
    -0.18703481171888114,
    0.030841381835986965,
    0.032883011666982945,
    -0.010597401784997278,
)

_WAVELETS: dict[str, tuple[float, ...]] = {
    "la8": LA8_LOWPASS,
    "db4": DB4_LOWPASS,
}


@dataclass(frozen=True)
class ModwtResult:
    """MODWT coefficients with the metadata needed to invert or mask them.

    ``details[j]`` holds the level-``j+1`` wavelet coefficients and
    ``scaling`` the level-``J`` scaling coefficients. Every array has the
    length of the (cropped) input series. ``boundary_widths[j]`` is the
    number of leading samples of level ``j+1`` that read the extension
    (zero for ``boundary="circular"`` semantics).
    """

    details: tuple[np.ndarray, ...]
    scaling: np.ndarray
    wavelet: str
    boundary: str
    boundary_widths: tuple[int, ...]
    length: int


@dataclass(frozen=True)
class MraResult:
    """Additive multiresolution analysis aligned with the input clock.

    ``details[j]`` and ``smooth`` sum to the original series exactly for
    both boundary modes; with ``boundary="reflection"`` the edge samples
    of each component see the mirrored extension.
    """

    details: tuple[np.ndarray, ...]
    smooth: np.ndarray
    wavelet: str
    boundary: str
    boundary_widths: tuple[int, ...]
    length: int


@dataclass(frozen=True)
class WaveletVariance:
    """Unbiased MODWT wavelet-variance estimates per level.

    ``variance[j]`` estimates nu-squared at level ``j+1`` from the samples
    outside each level's leading boundary zone (demeaned), ``counts[j]``
    is the number of contributing samples, and ``shares`` renormalizes the
    variances to sum to one.
    """

    variance: np.ndarray
    counts: np.ndarray
    shares: np.ndarray


def wavelet_names() -> tuple[str, ...]:
    """Return the supported filter-bank names."""
    return tuple(_WAVELETS)


def wavelet_filters(name: str) -> tuple[np.ndarray, np.ndarray]:
    """Return the (low-pass, high-pass) filter pair for ``name``.

    The high-pass filter is the quadrature-mirror companion
    ``g[k] = (-1)**k * h[L - 1 - k]``.
    """
    try:
        lowpass = np.asarray(_WAVELETS[name], dtype=np.float64)
    except KeyError as error:
        supported = ", ".join(sorted(_WAVELETS))
        raise ValueError(f"unknown wavelet {name!r}; supported: {supported}") from error
    taps = lowpass.size
    signs = np.where(np.arange(taps) % 2 == 0, 1.0, -1.0)
    highpass = signs * lowpass[::-1]
    return lowpass, highpass


def level_filter_width(level: int, base_length: int) -> int:
    """Return the level-``level`` equivalent filter width ``L_j``."""
    return (2**level - 1) * (base_length - 1) + 1


def max_level(length: int, wavelet: str = "la8") -> int:
    """Return the deepest level whose unbiased zone is still meaningful.

    A level is usable when its filter width fits in half the series, so
    boundary-free samples remain for variance estimation.
    """
    if length < 2:
        raise ValueError(f"series too short for MODWT: length={length}")
    lowpass, _ = wavelet_filters(wavelet)
    limit = length // 2
    deepest = 1
    while level_filter_width(deepest + 1, lowpass.size) <= limit:
        deepest += 1
    return deepest


def _resolve_level(
    length: int, level: int | None, wavelet: str, *, allow_overshoot: bool
) -> int:
    if level is None:
        return max_level(length, wavelet)
    if level < 1:
        raise ValueError(f"level must be >= 1, got {level}")
    lowpass, _ = wavelet_filters(wavelet)
    if not allow_overshoot and level_filter_width(level, lowpass.size) > length:
        raise ValueError(
            f"level {level} needs filter width "
            f"{level_filter_width(level, lowpass.size)} > length {length}"
        )
    return level


def _step_correlate(series: np.ndarray, taps: np.ndarray, step: int) -> np.ndarray:
    """Circular correlation with ``taps`` placed at multiples of ``step``.

    Computes ``y[t] = sum_k taps[k] * series[(t - step * k) mod n]``.
    """
    output = np.zeros(series.shape[0], dtype=np.float64)
    for index, coefficient in enumerate(taps):
        if coefficient != 0.0:
            output += coefficient * np.roll(series, step * index)
    return output


def modwt(
    values: object,
    *,
    wavelet: str = "la8",
    level: int | None = None,
    boundary: str = "reflection",
) -> ModwtResult:
    """Compute the MODWT pyramid for a 1-d series."""
    series = np.asarray(values, dtype=np.float64)
    if series.ndim != 1:
        raise ValueError("MODWT input must be one-dimensional")
    if not np.all(np.isfinite(series)):
        raise ValueError("MODWT input must be finite")
    length = series.size
    if boundary not in {"circular", "reflection"}:
        raise ValueError(
            f"boundary must be 'circular' or 'reflection', got {boundary!r}"
        )
    lowpass, highpass = wavelet_filters(wavelet)
    resolved = _resolve_level(
        length, level, wavelet, allow_overshoot=boundary == "reflection"
    )
    scaled_low = lowpass / _SQRT2
    scaled_high = highpass / _SQRT2
    widths = tuple(
        level_filter_width(index + 1, lowpass.size) - 1 for index in range(resolved)
    )
    pad = widths[-1] if boundary == "reflection" else 0
    working = (
        np.pad(series, pad, mode="reflect") if boundary == "reflection" else series
    )
    details: list[np.ndarray] = []
    for index in range(resolved):
        step = 2**index
        detail = _step_correlate(working, scaled_high, step)
        working = _step_correlate(working, scaled_low, step)
        if pad:
            detail = detail[pad : pad + length]
        details.append(detail)
    if pad:
        smooth = working[pad : pad + length]
    else:
        smooth = working
    return ModwtResult(
        details=tuple(details),
        scaling=smooth,
        wavelet=wavelet,
        boundary=boundary,
        boundary_widths=widths,
        length=length,
    )


def _synthesis_step(
    detail: np.ndarray,
    scaling: np.ndarray,
    scaled_low: np.ndarray,
    scaled_high: np.ndarray,
    step: int,
) -> np.ndarray:
    coarser = _step_correlate(detail, scaled_high, -step)
    coarser += _step_correlate(scaling, scaled_low, -step)
    return coarser


def imodwt(result: ModwtResult) -> np.ndarray:
    """Reconstruct the series from MODWT coefficients.

    Exact for ``boundary="circular"``. For ``boundary="reflection"`` the
    reconstruction is exact up to the last ``boundary_widths[-1]`` samples
    and carries circular-wrap error on the trailing zone; use
    :func:`modwt_mra` when an exact additive reconstruction is needed.
    """
    lowpass, highpass = wavelet_filters(result.wavelet)
    scaled_low = lowpass / _SQRT2
    scaled_high = highpass / _SQRT2
    scaling = result.scaling.copy()
    for index in range(len(result.details) - 1, -1, -1):
        step = 2**index
        scaling = _synthesis_step(
            result.details[index], scaling, scaled_low, scaled_high, step
        )
    return scaling


def _detail_contribution(
    detail: np.ndarray,
    scaled_low: np.ndarray,
    scaled_high: np.ndarray,
    level_index: int,
) -> np.ndarray:
    """Propagate level-``level_index`` wavelet coefficients down the pyramid."""
    series = _step_correlate(detail, scaled_high, -(2**level_index))
    for lower in range(level_index - 1, -1, -1):
        series = _step_correlate(series, scaled_low, -(2**lower))
    return series


def mra_from_modwt(result: ModwtResult) -> MraResult:
    """Build the additive MRA from MODWT coefficients.

    Detail ``j`` is the contribution of wavelet coefficients ``W_j`` to the
    reconstruction: each detail is synthesized down the pyramid with the
    level-``j`` high-pass adjoint followed by low-pass adjoints to the
    base level. The smooth is the contribution of the scaling
    coefficients. Details are zero-phase aligned for the symmetric filter
    banks shipped here.
    """
    lowpass, highpass = wavelet_filters(result.wavelet)
    scaled_low = lowpass / _SQRT2
    scaled_high = highpass / _SQRT2
    details = tuple(
        _detail_contribution(detail, scaled_low, scaled_high, index)
        for index, detail in enumerate(result.details)
    )
    smooth = result.scaling
    for lower in range(len(result.details) - 1, -1, -1):
        smooth = _step_correlate(smooth, scaled_low, -(2**lower))
    return MraResult(
        details=details,
        smooth=smooth,
        wavelet=result.wavelet,
        boundary=result.boundary,
        boundary_widths=result.boundary_widths,
        length=result.length,
    )


def modwt_mra(
    values: object,
    *,
    wavelet: str = "la8",
    level: int | None = None,
    boundary: str = "reflection",
) -> MraResult:
    """One-call additive multiresolution analysis.

    For ``boundary="reflection"`` the analysis and synthesis both run on
    the reflected extension before cropping, so the returned components
    sum to the input exactly at every sample and each component's edge
    samples see the mirrored extension rather than a circular wrap to the
    opposite end of the series.
    """
    series = np.asarray(values, dtype=np.float64)
    if series.ndim != 1 or not np.all(np.isfinite(series)):
        raise ValueError("MRA input must be a finite one-dimensional series")
    length = series.size
    if boundary not in {"circular", "reflection"}:
        raise ValueError(
            f"boundary must be 'circular' or 'reflection', got {boundary!r}"
        )
    lowpass, _ = wavelet_filters(wavelet)
    resolved = _resolve_level(
        length, level, wavelet, allow_overshoot=boundary == "reflection"
    )
    pad = (
        level_filter_width(resolved, lowpass.size) - 1
        if boundary == "reflection"
        else 0
    )
    working = np.pad(series, pad, mode="reflect") if pad else series
    coefficients = modwt(working, wavelet=wavelet, level=resolved, boundary="circular")
    mra = mra_from_modwt(coefficients)
    if pad:
        details = tuple(detail[pad : pad + length] for detail in mra.details)
        smooth = mra.smooth[pad : pad + length]
    else:
        details = mra.details
        smooth = mra.smooth
    widths = tuple(
        level_filter_width(index + 1, lowpass.size) - 1 for index in range(resolved)
    )
    return MraResult(
        details=details,
        smooth=smooth,
        wavelet=wavelet,
        boundary=boundary,
        boundary_widths=widths,
        length=length,
    )


def valid_mask(width: int, boundary_width: int) -> np.ndarray:
    """Boolean mask of samples outside a level's leading boundary zone."""
    mask = np.ones(width, dtype=bool)
    mask[:boundary_width] = False
    return mask


def wavelet_variance(result: ModwtResult) -> WaveletVariance:
    """Unbiased wavelet-variance estimates from a MODWT.

    Each level's variance is the demeaned mean square of its coefficients
    over samples after the leading boundary zone (WMTSA convention,
    ``M_j = N - L_j + 1``). Levels whose boundary zone consumes every
    sample return ``nan`` with count zero.
    """
    variances = np.empty(len(result.details), dtype=np.float64)
    counts = np.empty(len(result.details), dtype=np.int64)
    for index, detail in enumerate(result.details):
        mask = valid_mask(result.length, result.boundary_widths[index])
        samples = detail[mask]
        counts[index] = samples.size
        if samples.size == 0:
            variances[index] = np.nan
            continue
        centered = samples - samples.mean()
        variances[index] = float(np.mean(np.square(centered)))
    total = np.nansum(variances)
    if total > 0.0:
        shares = variances / total
    else:
        shares = np.full(variances.shape, np.nan)
    return WaveletVariance(variance=variances, counts=counts, shares=shares)
