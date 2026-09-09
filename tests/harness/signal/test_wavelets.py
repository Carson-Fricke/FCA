"""Behavior of the harness.signal MODWT wavelet kernels."""

from __future__ import annotations

import numpy as np
import pytest

from harness.signal.wavelets import (
    DB4_LOWPASS,
    LA8_LOWPASS,
    imodwt,
    level_filter_width,
    max_level,
    modwt,
    modwt_mra,
    mra_from_modwt,
    valid_mask,
    wavelet_filters,
    wavelet_names,
    wavelet_variance,
)


def _highpass(lowpass: np.ndarray) -> np.ndarray:
    taps = lowpass.size
    signs = np.where(np.arange(taps) % 2 == 0, 1.0, -1.0)
    return signs * lowpass[::-1]


@pytest.mark.parametrize("lowpass", [LA8_LOWPASS, DB4_LOWPASS])
def test_filter_banks_satisfy_qmf_relations(lowpass: tuple[float, ...]) -> None:
    low = np.asarray(lowpass, dtype=np.float64)
    high = _highpass(low)
    taps = low.size
    assert np.isclose(low.sum(), np.sqrt(2.0), atol=1e-12)
    assert np.isclose(high.sum(), 0.0, atol=5e-12)
    assert np.isclose(np.sum(low * low), 1.0, atol=1e-12)
    assert np.isclose(np.sum(high * high), 1.0, atol=1e-12)
    assert np.isclose(np.sum(low * ((-1.0) ** np.arange(taps))), 0.0, atol=5e-12)
    assert np.isclose(
        np.sum(high * ((-1.0) ** np.arange(taps))), np.sqrt(2.0), atol=1e-12
    )
    for lag in range(2, taps, 2):
        assert abs(np.sum(low[:-lag] * low[lag:])) < 1e-12
        assert abs(np.sum(high[:-lag] * high[lag:])) < 1e-12
    for shift in range(-taps + 2, taps - 1, 2):
        first = np.arange(max(0, shift), taps + min(0, shift))
        overlap = float(np.sum(low[first - shift] * high[first]))
        assert abs(overlap) < 1e-12


def test_wavelet_names_and_unknown_filter_rejected() -> None:
    assert "la8" in wavelet_names()
    assert "db4" in wavelet_names()
    with pytest.raises(ValueError, match="unknown wavelet"):
        wavelet_filters("haar9")


def test_level_filter_widths_match_wmtsa_table() -> None:
    assert level_filter_width(1, 8) == 8
    assert level_filter_width(2, 8) == 22
    assert level_filter_width(3, 8) == 50
    assert level_filter_width(4, 8) == 106
    assert level_filter_width(6, 8) == 442


def test_max_level_keeps_boundary_free_samples() -> None:
    assert max_level(390, "la8") == 4
    assert max_level(1526, "la8") == 6
    with pytest.raises(ValueError, match="too short"):
        max_level(1, "la8")


@pytest.mark.parametrize("wavelet", ["la8", "db4"])
@pytest.mark.parametrize("length", [390, 1526, 100])
def test_circular_modwt_is_exact_and_energy_preserving(
    wavelet: str, length: int
) -> None:
    rng = np.random.default_rng(11)
    series = rng.standard_normal(length)
    result = modwt(series, wavelet=wavelet, boundary="circular")
    assert np.allclose(imodwt(result), series, atol=1e-9)
    mra = mra_from_modwt(result)
    total = sum(mra.details, start=mra.smooth * 0.0) + mra.smooth
    assert np.allclose(total, series, atol=1e-9)
    energy = float(np.sum(series * series))
    coefficients_energy = sum(float(np.sum(d * d)) for d in result.details)
    coefficients_energy += float(np.sum(result.scaling * result.scaling))
    assert np.isclose(coefficients_energy, energy, rtol=1e-8)


@pytest.mark.parametrize("wavelet", ["la8", "db4"])
@pytest.mark.parametrize("length", [390, 1526, 100, 2])
def test_reflection_mra_reconstructs_exactly(wavelet: str, length: int) -> None:
    rng = np.random.default_rng(12)
    series = rng.standard_normal(length)
    mra = modwt_mra(series, wavelet=wavelet, boundary="reflection")
    total = sum(mra.details, start=mra.smooth * 0.0) + mra.smooth
    assert np.allclose(total, series, atol=1e-9)


def test_circular_modwt_is_shift_invariant() -> None:
    rng = np.random.default_rng(13)
    series = rng.standard_normal(256)
    shift = 7
    base = modwt(series, wavelet="la8", boundary="circular", level=4)
    moved = modwt(np.roll(series, shift), wavelet="la8", boundary="circular", level=4)
    for level in range(4):
        assert np.allclose(
            np.roll(base.details[level], shift),
            moved.details[level],
            atol=1e-10,
        )
    assert np.allclose(np.roll(base.scaling, shift), moved.scaling, atol=1e-10)


def test_sine_energy_localizes_to_matching_octave() -> None:
    grid = np.arange(1024)
    for period, expected_level in ((32.0 / 3.0, 3), (64.0 / 3.0, 4)):
        sine = np.sin(2.0 * np.pi * grid / period)
        result = modwt(sine, wavelet="la8", boundary="circular", level=6)
        energies = [float(np.mean(d * d)) for d in result.details]
        assert int(np.argmax(energies)) + 1 == expected_level
    band_edge = np.sin(2.0 * np.pi * grid / 16.0)
    result = modwt(band_edge, wavelet="la8", boundary="circular", level=6)
    energies = [float(np.mean(d * d)) for d in result.details]
    assert abs(energies[2] - energies[3]) < 1e-6


def test_reflection_imodwt_exact_before_trailing_boundary() -> None:
    rng = np.random.default_rng(14)
    series = rng.standard_normal(390)
    result = modwt(series, wavelet="la8", boundary="reflection", level=4)
    width = result.boundary_widths[-1]
    head = imodwt(result)[: 390 - width]
    assert np.allclose(head, series[: 390 - width], atol=1e-9)


def test_wavelet_variance_matches_wmtsa_counts() -> None:
    rng = np.random.default_rng(15)
    series = rng.standard_normal(390)
    result = modwt(series, wavelet="la8", boundary="reflection", level=4)
    variance = wavelet_variance(result)
    assert variance.counts.tolist() == [383, 369, 341, 285]
    assert np.isclose(np.nansum(variance.shares), 1.0)
    for index, width in enumerate(result.boundary_widths):
        mask = valid_mask(390, width)
        samples = result.details[index][mask]
        centered = samples - samples.mean()
        assert np.isclose(variance.variance[index], float(np.mean(np.square(centered))))


def test_white_noise_variance_partitions_by_octave_bandwidth() -> None:
    rng = np.random.default_rng(16)
    series = rng.standard_normal(4096)
    result = modwt(series, wavelet="la8", boundary="reflection", level=6)
    variance = wavelet_variance(result)
    expected = 0.5 ** np.arange(1, 7)
    assert np.allclose(variance.variance, expected, rtol=0.35)
    energy = float(np.mean(np.square(series)))
    assert np.isclose(variance.variance.sum() + result.scaling.var(), energy, rtol=0.1)


def test_modwt_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="one-dimensional"):
        modwt(np.zeros((3, 3)))
    with pytest.raises(ValueError, match="finite"):
        modwt(np.array([1.0, np.nan, 2.0]))
    with pytest.raises(ValueError, match="boundary must be"):
        modwt(np.zeros(64), boundary="zero")
    with pytest.raises(ValueError, match="level must be"):
        modwt(np.zeros(64), level=0)
    with pytest.raises(ValueError, match="filter width"):
        modwt(np.zeros(7), boundary="circular", level=1)
