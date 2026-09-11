//! Analytic-signal kernels for band-limited series.
//!
//! Port of `harness.signal.analytic`. Given a real band-limited series, the
//! analytic signal `z = x + i * Hilbert(x)` yields the instantaneous envelope
//! `|z|` and phase `arg(z)`. The Hilbert transform is computed with an FFT and
//! is circular, so phase and envelope near the edges inherit the same boundary
//! caveat as the underlying wavelet coefficients.

use crate::signal::fft::{dft, idft};
use crate::signal::wavelets::SignalError;

/// Return the (envelope, phase) of a real one-dimensional series.
///
/// # Errors
///
/// Returns [`SignalError::InvalidInput`] for an empty or non-finite series.
pub fn analytic_signal(values: &[f64]) -> Result<(Vec<f64>, Vec<f64>), SignalError> {
    if values.is_empty() || values.iter().any(|value| !value.is_finite()) {
        return Err(SignalError::InvalidInput(
            "analytic input must be a finite non-empty series".to_string(),
        ));
    }
    let analytic = hilbert(values);
    let mut envelope = Vec::with_capacity(analytic.len());
    let mut phase = Vec::with_capacity(analytic.len());
    for (re, im) in analytic {
        envelope.push((re * re + im * im).sqrt());
        phase.push(im.atan2(re));
    }
    Ok((envelope, phase))
}

fn hilbert(values: &[f64]) -> Vec<(f64, f64)> {
    let count = values.len();
    let zeros = vec![0.0f64; count];
    let (re, im) = dft(values, &zeros);
    let mut weights = vec![0.0f64; count];
    if count.is_multiple_of(2) {
        weights[0] = 1.0;
        weights[count / 2] = 1.0;
        for weight in weights.iter_mut().take(count / 2).skip(1) {
            *weight = 2.0;
        }
    } else {
        weights[0] = 1.0;
        for weight in weights.iter_mut().take(count.div_ceil(2)).skip(1) {
            *weight = 2.0;
        }
    }
    let spectral_re: Vec<f64> = re
        .iter()
        .zip(weights.iter())
        .map(|(value, weight)| value * weight)
        .collect();
    let spectral_im: Vec<f64> = im
        .iter()
        .zip(weights.iter())
        .map(|(value, weight)| value * weight)
        .collect();
    let (out_re, out_im) = idft(&spectral_re, &spectral_im);
    out_re.into_iter().zip(out_im).collect()
}

/// Per-sample phase velocity from a phase series.
#[must_use]
pub fn instantaneous_phase_velocity(phase: &[f64]) -> Vec<f64> {
    let unwrapped = unwrap(phase);
    if unwrapped.len() <= 1 {
        return vec![0.0; unwrapped.len()];
    }
    let mut velocity = vec![0.0f64; unwrapped.len()];
    for index in 1..unwrapped.len() {
        velocity[index] = unwrapped[index] - unwrapped[index - 1];
    }
    velocity[0] = velocity[1];
    velocity
}

/// Mean resultant length of successive phase increments.
#[must_use]
pub fn phase_coherence(phase: &[f64]) -> f64 {
    let finite: Vec<f64> = phase
        .iter()
        .copied()
        .filter(|value| value.is_finite())
        .collect();
    if finite.len() < 2 {
        return f64::NAN;
    }
    let unwrapped = unwrap(&finite);
    let mut sum_re = 0.0f64;
    let mut sum_im = 0.0f64;
    for index in 1..unwrapped.len() {
        let increment = unwrapped[index] - unwrapped[index - 1];
        sum_re += increment.cos();
        sum_im += increment.sin();
    }
    let count = (unwrapped.len() - 1) as f64;
    ((sum_re / count).hypot(sum_im / count)).min(1.0)
}

/// Per-block mean phase velocity and coherence.
#[must_use]
pub fn block_phase_features(phase: &[f64], blocks: &[(usize, usize)]) -> (Vec<f64>, Vec<f64>) {
    let velocity = instantaneous_phase_velocity(phase);
    let mut speeds = vec![f64::NAN; blocks.len()];
    let mut coherences = vec![f64::NAN; blocks.len()];
    for (index, (start, stop)) in blocks.iter().enumerate() {
        let start = (*start).min(phase.len());
        let stop = (*stop).min(phase.len());
        if stop.saturating_sub(start) < 2 {
            continue;
        }
        let samples = &velocity[start..stop];
        let finite: Vec<f64> = samples
            .iter()
            .copied()
            .filter(|value| value.is_finite())
            .collect();
        if finite.is_empty() {
            continue;
        }
        speeds[index] = finite.iter().sum::<f64>() / finite.len() as f64;
        coherences[index] = phase_coherence(&phase[start..stop]);
    }
    (speeds, coherences)
}

/// Unwrap a phase series, matching `numpy.unwrap` with the default period.
#[must_use]
pub fn unwrap(phase: &[f64]) -> Vec<f64> {
    let count = phase.len();
    if count == 0 {
        return Vec::new();
    }
    let two_pi = std::f64::consts::TAU;
    let mut result = vec![0.0f64; count];
    result[0] = phase[0];
    let mut correction = 0.0f64;
    for index in 1..count {
        let difference = phase[index] - phase[index - 1];
        if difference > std::f64::consts::PI {
            correction -= two_pi;
        } else if difference < -std::f64::consts::PI {
            correction += two_pi;
        }
        result[index] = phase[index] + correction;
    }
    result
}

#[cfg(test)]
#[path = "../../tests/unit/signal_analytic.rs"]
mod tests;
