//! Morlet continuous wavelet transform for exploratory scalograms.
//!
//! Port of `harness.signal.scalogram`. Computes the complex CWT with the
//! analytic Morlet mother wavelet via zero-padded FFT correlation. The CWT is
//! redundant and non-orthogonal and is used only as an exploratory
//! time-frequency artifact.

use std::f64::consts::PI;

use crate::signal::fft::{fft_in_place, ifft_in_place};
use crate::signal::wavelets::SignalError;

/// Default Morlet center frequency.
pub const DEFAULT_OMEGA0: f64 = 6.0;

/// A complex number used by the scalogram kernel.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Complex {
    /// Real part.
    pub re: f64,
    /// Imaginary part.
    pub im: f64,
}

fn cone_constant() -> f64 {
    let omega = DEFAULT_OMEGA0;
    4.0 * PI / (omega + (2.0 + omega * omega).sqrt())
}

/// Center period of each Morlet scale for unit sample spacing.
#[must_use]
pub fn morlet_periods(scales: &[f64]) -> Vec<f64> {
    let constant = cone_constant();
    scales.iter().map(|scale| constant * scale).collect()
}

/// Log-spaced Morlet scales covering `[min_period, max_period]`.
///
/// # Errors
///
/// Returns [`SignalError::InvalidInput`] when the period range or voice count
/// is invalid.
pub fn log_scales(
    min_period: f64,
    max_period: f64,
    voices_per_octave: usize,
) -> Result<Vec<f64>, SignalError> {
    if min_period <= 0.0 || max_period <= min_period {
        return Err(SignalError::InvalidInput(format!(
            "need 0 < min_period < max_period, got {min_period}, {max_period}"
        )));
    }
    if voices_per_octave < 1 {
        return Err(SignalError::InvalidInput(
            "voices_per_octave must be >= 1".to_string(),
        ));
    }
    let base = cone_constant();
    let low = (min_period / base).log2();
    let high = (max_period / base).log2();
    let count = ((high - low) * voices_per_octave as f64).floor() as i64 + 1;
    let count = count.max(0) as usize;
    Ok((0..count)
        .map(|index| {
            let exponent = low + index as f64 / voices_per_octave as f64;
            2.0f64.powf(exponent)
        })
        .collect())
}

/// Boolean mask of samples outside the cone of influence per scale.
#[must_use]
pub fn cone_mask(width: usize, scales: &[f64], edge: f64) -> Vec<Vec<bool>> {
    let periods = morlet_periods(scales);
    periods
        .iter()
        .map(|period| {
            let half = edge * period / 2.0;
            (0..width)
                .map(|index| {
                    let position = index as f64;
                    position >= half && position <= (width as f64 - 1.0) - half
                })
                .collect()
        })
        .collect()
}

/// Complex Morlet CWT of a real one-dimensional series.
///
/// # Errors
///
/// Returns [`SignalError::InvalidInput`] for empty/non-finite input or invalid
/// scales.
pub fn morlet_cwt(
    values: &[f64],
    scales: &[f64],
    omega0: f64,
) -> Result<Vec<Vec<Complex>>, SignalError> {
    if values.is_empty() || values.iter().any(|value| !value.is_finite()) {
        return Err(SignalError::InvalidInput(
            "CWT input must be a finite non-empty series".to_string(),
        ));
    }
    if scales.is_empty() || scales.iter().any(|scale| *scale <= 0.0) {
        return Err(SignalError::InvalidInput(
            "scales must be a non-empty positive array".to_string(),
        ));
    }
    let count = values.len();
    let mut coefficients = Vec::with_capacity(scales.len());
    for scale in scales {
        let reach = (omega0 * scale).ceil() as i64;
        let kernel_length = (2 * reach + 1) as usize;
        let normalizer = scale.sqrt() * PI.powf(0.25);
        let mut kernel_re = vec![0.0f64; kernel_length];
        let mut kernel_im = vec![0.0f64; kernel_length];
        for offset in 0..kernel_length {
            let grid = (offset as i64 - reach) as f64;
            let envelope = (-0.5 * (grid / scale) * (grid / scale)).exp() / normalizer;
            let angle = omega0 * grid / scale;
            kernel_re[offset] = angle.cos() * envelope;
            kernel_im[offset] = angle.sin() * envelope;
        }
        // reversed_kernel = conj(mother[::-1])
        let mut reversed_re = vec![0.0f64; kernel_length];
        let mut reversed_im = vec![0.0f64; kernel_length];
        for offset in 0..kernel_length {
            reversed_re[offset] = kernel_re[kernel_length - 1 - offset];
            reversed_im[offset] = -kernel_im[kernel_length - 1 - offset];
        }
        let mut size = 1usize;
        while size < count + kernel_length - 1 {
            size *= 2;
        }
        let mut series_re = vec![0.0f64; size];
        let mut series_im = vec![0.0f64; size];
        series_re[..count].copy_from_slice(values);
        let mut embedded_re = vec![0.0f64; size];
        let mut embedded_im = vec![0.0f64; size];
        embedded_re[..kernel_length].copy_from_slice(&reversed_re);
        embedded_im[..kernel_length].copy_from_slice(&reversed_im);
        fft_in_place(&mut series_re, &mut series_im);
        fft_in_place(&mut embedded_re, &mut embedded_im);
        let mut product_re = vec![0.0f64; size];
        let mut product_im = vec![0.0f64; size];
        for index in 0..size {
            product_re[index] =
                series_re[index] * embedded_re[index] - series_im[index] * embedded_im[index];
            product_im[index] =
                series_re[index] * embedded_im[index] + series_im[index] * embedded_re[index];
        }
        ifft_in_place(&mut product_re, &mut product_im);
        let start = kernel_length - 1;
        let mut row = Vec::with_capacity(count);
        for index in 0..count {
            row.push(Complex {
                re: product_re[start + index],
                im: product_im[start + index],
            });
        }
        coefficients.push(row);
    }
    Ok(coefficients)
}

/// A wavelet power scalogram and its cone-of-influence mask.
pub type Scalogram = (Vec<Vec<f64>>, Vec<Vec<bool>>);

/// Wavelet power scalogram and its cone-of-influence mask.
///
/// # Errors
///
/// Returns [`SignalError`] under the same conditions as [`morlet_cwt`].
pub fn scalogram(values: &[f64], scales: &[f64], omega0: f64) -> Result<Scalogram, SignalError> {
    let coefficients = morlet_cwt(values, scales, omega0)?;
    let power = coefficients
        .iter()
        .map(|row| {
            row.iter()
                .map(|value| value.re * value.re + value.im * value.im)
                .collect()
        })
        .collect();
    Ok((power, cone_mask(values.len(), scales, 2.0)))
}

#[cfg(test)]
#[path = "../../tests/unit/signal_scalogram.rs"]
mod tests;
