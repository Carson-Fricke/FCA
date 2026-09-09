"""Behavior of the harness.signal Morlet CWT scalogram kernels."""

from __future__ import annotations

import numpy as np
import pytest

from harness.signal.scalogram import (
    DEFAULT_OMEGA0,
    log_scales,
    morlet_cwt,
    morlet_periods,
    scalogram,
)


def test_morlet_cwt_matches_linear_convolution() -> None:
    series = np.random.default_rng(3).standard_normal(390)
    scales = log_scales(2.0, 96.0, voices_per_octave=12)
    count = series.size
    for scale in (scales[0], scales[10], scales[30], scales[-1]):
        reach = int(np.ceil(DEFAULT_OMEGA0 * scale))
        grid = np.arange(-reach, reach + 1)
        mother = (
            np.exp(1j * DEFAULT_OMEGA0 * grid / scale)
            * np.exp(-0.5 * np.square(grid / scale))
            / (np.sqrt(scale) * np.pi**0.25)
        )
        kernel = np.conj(mother[::-1])
        reference = np.convolve(series, kernel, mode="full")[
            kernel.size - 1 : kernel.size - 1 + count
        ]
        computed = morlet_cwt(series, np.array([scale]))[0]
        assert np.allclose(computed, reference, atol=1e-9)


def test_sine_power_peaks_at_matching_scale() -> None:
    grid = np.arange(512)
    scales = log_scales(2.0, 96.0, voices_per_octave=12)
    power, _ = scalogram(np.sin(2.0 * np.pi * grid / 24.0), scales)
    best = int(np.argmax(power[:, 100:400].mean(axis=1)))
    estimated = float(morlet_periods(scales)[best])
    assert abs(np.log2(estimated / 24.0)) <= 1.0 / 12.0 + 1e-9


def test_sine_phase_tracks_input_phase_at_matching_scale() -> None:
    grid = np.arange(512)
    scales = log_scales(16.0, 40.0, voices_per_octave=24)
    periods = morlet_periods(scales)
    best = int(np.argmin(np.abs(periods - 24.0)))
    sine = np.sin(2.0 * np.pi * grid / 24.0)
    cosine = np.cos(2.0 * np.pi * grid / 24.0)
    sine_phase = np.angle(morlet_cwt(sine, scales)[best])[100:400]
    cosine_phase = np.angle(morlet_cwt(cosine, scales)[best])[100:400]
    relative = np.angle(np.exp(1j * (cosine_phase - sine_phase)))
    assert abs(relative.mean() - np.pi / 2.0) < 0.05
    assert relative.std() < 0.05


def test_cone_mask_excludes_edges_at_long_scales() -> None:
    scales = np.array([2.0, 32.0, 128.0])
    _, mask = scalogram(np.random.default_rng(5).standard_normal(256), scales)
    assert mask[0].mean() > 0.9
    assert mask[1].mean() < 0.9
    assert mask[2].mean() < 0.5


def test_log_scales_span_requested_periods() -> None:
    scales = log_scales(2.0, 96.0, voices_per_octave=12)
    periods = morlet_periods(scales)
    assert periods[0] >= 2.0
    assert periods[-1] <= 96.0 * 2.0 ** (1.0 / 12.0)
    assert np.all(np.diff(scales) > 0.0)


def test_scalogram_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="one-dimensional"):
        morlet_cwt(np.zeros((2, 2)), np.array([4.0]))
    with pytest.raises(ValueError, match="finite"):
        morlet_cwt(np.array([1.0, np.nan]), np.array([4.0]))
    with pytest.raises(ValueError, match="scales must be"):
        morlet_cwt(np.zeros(64), np.array([0.0]))
    with pytest.raises(ValueError, match="min_period"):
        log_scales(0.0, 32.0)
    with pytest.raises(ValueError, match="voices_per_octave"):
        log_scales(2.0, 32.0, voices_per_octave=0)
