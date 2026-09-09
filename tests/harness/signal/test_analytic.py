"""Behavior of the harness.signal analytic-signal kernels."""

from __future__ import annotations

import numpy as np
import pytest

from harness.signal.analytic import (
    analytic_signal,
    block_phase_features,
    instantaneous_phase_velocity,
    phase_coherence,
)


def test_sine_envelope_and_phase_velocity_are_exact() -> None:
    count = 2048
    grid = np.arange(count)
    amplitude = 2.5
    cycles = 40
    sine = amplitude * np.sin(2.0 * np.pi * cycles * grid / count)
    envelope, phase = analytic_signal(sine)
    interior = slice(50, count - 50)
    assert np.isclose(envelope[interior].mean(), amplitude, atol=1e-9)
    velocity = np.diff(np.unwrap(phase))[interior]
    expected = 2.0 * np.pi * cycles / count
    assert np.isclose(velocity.mean(), expected, atol=1e-12)


def test_am_signal_envelope_tracks_the_modulation() -> None:
    count = 4096
    grid = np.arange(count)
    modulator = 1.0 + 0.5 * np.cos(2.0 * np.pi * 4.0 * grid / count)
    carrier = np.cos(2.0 * np.pi * 200.0 * grid / count)
    envelope, _ = analytic_signal(modulator * carrier)
    interior = slice(100, count - 100)
    correlation = np.corrcoef(envelope[interior], modulator[interior])[0, 1]
    assert correlation > 0.999


def test_dc_input_carries_no_oscillation() -> None:
    envelope, phase = analytic_signal(np.full(64, 3.0))
    assert np.allclose(envelope, 3.0)
    assert np.allclose(phase, 0.0)


def test_instantaneous_phase_velocity_of_a_ramp() -> None:
    ramp = np.unwrap(2.0 * np.pi * np.arange(100) / 10.0)
    velocity = instantaneous_phase_velocity(ramp)
    assert np.isclose(velocity[1:].mean(), 2.0 * np.pi / 10.0)
    single = instantaneous_phase_velocity(np.array([1.0]))
    assert single[0] == 0.0


def test_phase_coherence_separates_tone_from_noise() -> None:
    steady = np.unwrap(2.0 * np.pi * np.arange(500) / 10.0)
    assert phase_coherence(steady) > 0.999
    rng = np.random.default_rng(4)
    noise = rng.uniform(-np.pi, np.pi, 500)
    assert phase_coherence(noise) < 0.2
    assert np.isnan(phase_coherence(np.array([0.5])))


def test_block_phase_features_match_manual_computation() -> None:
    steady = np.unwrap(2.0 * np.pi * np.arange(100) / 10.0)
    speeds, coherences = block_phase_features(steady, [(0, 50), (50, 100)])
    assert np.allclose(speeds, 2.0 * np.pi / 10.0)
    assert np.allclose(coherences, 1.0)
    empty_speeds, empty_coherences = block_phase_features(steady, [(200, 260)])
    assert np.isnan(empty_speeds[0])
    assert np.isnan(empty_coherences[0])


def test_analytic_signal_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="one-dimensional"):
        analytic_signal(np.zeros((2, 2)))
    with pytest.raises(ValueError, match="finite"):
        analytic_signal(np.array([1.0, np.inf]))
