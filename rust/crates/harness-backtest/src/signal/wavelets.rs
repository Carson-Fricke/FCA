//! Maximal-overlap discrete wavelet transform kernels.
//!
//! Port of `harness.signal.wavelets`: the circular MODWT pyramid with
//! least-asymmetric (LA8) and Daubechies (DB4) filter banks, the additive
//! multiresolution analysis, the exact inverse transform, and unbiased
//! wavelet-variance estimates. Reflection padding mirrors `numpy.pad(...,
//! mode="reflect")`; boundary handling matches the Python docstrings.

/// Boundary handling for the transform.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Boundary {
    /// Treat the signal as one period.
    Circular,
    /// Reflect-pad by the widest level filter and crop back.
    Reflection,
}

impl Boundary {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Circular => "circular",
            Self::Reflection => "reflection",
        }
    }

    /// Parse a boundary mode from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "circular" => Some(Self::Circular),
            "reflection" => Some(Self::Reflection),
            _ => None,
        }
    }
}

/// A wavelet kernel failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SignalError {
    /// The wavelet name is unknown.
    UnknownWavelet(String),
    /// The input is empty, multi-dimensional, or non-finite.
    InvalidInput(String),
    /// A level was zero or its filter exceeds the series.
    InvalidLevel(String),
}

impl std::fmt::Display for SignalError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownWavelet(name) => write!(formatter, "unknown wavelet {name:?}"),
            Self::InvalidInput(message) | Self::InvalidLevel(message) => {
                formatter.write_str(message)
            }
        }
    }
}

impl std::error::Error for SignalError {}

/// LA8 least-asymmetric low-pass filter.
pub const LA8_LOWPASS: [f64; 8] = [
    -0.075_765_714_789_273_33,
    -0.029_635_527_645_998_51,
    0.497_618_667_632_015_45,
    0.803_738_751_805_916_1,
    0.297_857_795_605_277_36,
    -0.099_219_543_576_847_22,
    -0.012_603_967_262_037_833,
    0.032_223_100_604_042_7,
];

/// DB4 Daubechies low-pass filter.
pub const DB4_LOWPASS: [f64; 8] = [
    0.230_377_813_308_855_23,
    0.714_846_570_552_541_5,
    0.630_880_767_929_590_4,
    -0.027_983_769_416_983_85,
    -0.187_034_811_718_881_14,
    0.030_841_381_835_986_965,
    0.032_883_011_666_982_945,
    -0.010_597_401_784_997_278,
];

/// MODWT coefficients with the metadata needed to invert or mask them.
#[derive(Clone, Debug, PartialEq)]
pub struct ModwtResult {
    /// `details[j]` holds the level-`j+1` wavelet coefficients.
    pub details: Vec<Vec<f64>>,
    /// The level-`J` scaling coefficients.
    pub scaling: Vec<f64>,
    /// Wavelet name.
    pub wavelet: String,
    /// Boundary mode.
    pub boundary: Boundary,
    /// Leading extension-reading samples per level.
    pub boundary_widths: Vec<usize>,
    /// Cropped series length.
    pub length: usize,
}

/// Additive multiresolution analysis aligned with the input clock.
#[derive(Clone, Debug, PartialEq)]
pub struct MraResult {
    /// Detail contributions per level.
    pub details: Vec<Vec<f64>>,
    /// Smooth contribution.
    pub smooth: Vec<f64>,
    /// Wavelet name.
    pub wavelet: String,
    /// Boundary mode.
    pub boundary: Boundary,
    /// Leading extension-reading samples per level.
    pub boundary_widths: Vec<usize>,
    /// Cropped series length.
    pub length: usize,
}

/// Unbiased MODWT wavelet-variance estimates per level.
#[derive(Clone, Debug, PartialEq)]
pub struct WaveletVariance {
    /// Variance per level (`NaN` when the valid zone is empty).
    pub variance: Vec<f64>,
    /// Contributing sample count per level.
    pub counts: Vec<usize>,
    /// Variances renormalized to sum to one (`NaN` when the total is zero).
    pub shares: Vec<f64>,
}

/// Return the supported filter-bank names.
#[must_use]
pub fn wavelet_names() -> [&'static str; 2] {
    ["la8", "db4"]
}

/// Return the (low-pass, high-pass) filter pair for `name`.
///
/// # Errors
///
/// Returns [`SignalError::UnknownWavelet`] for an unregistered name.
pub fn wavelet_filters(name: &str) -> Result<(Vec<f64>, Vec<f64>), SignalError> {
    let lowpass: &[f64] = match name {
        "la8" => &LA8_LOWPASS,
        "db4" => &DB4_LOWPASS,
        other => return Err(SignalError::UnknownWavelet(other.to_string())),
    };
    let taps = lowpass.len();
    let mut highpass = vec![0.0f64; taps];
    for index in 0..taps {
        let sign = if index % 2 == 0 { 1.0 } else { -1.0 };
        highpass[index] = sign * lowpass[taps - 1 - index];
    }
    Ok((lowpass.to_vec(), highpass))
}

/// Return the level-`level` equivalent filter width `L_j`.
#[must_use]
pub fn level_filter_width(level: usize, base_length: usize) -> usize {
    ((1usize << level) - 1) * (base_length - 1) + 1
}

/// Return the deepest level whose unbiased zone is still meaningful.
///
/// # Errors
///
/// Returns [`SignalError`] when the series is shorter than two samples or the
/// wavelet is unknown.
pub fn max_level(length: usize, wavelet: &str) -> Result<usize, SignalError> {
    if length < 2 {
        return Err(SignalError::InvalidInput(format!(
            "series too short for MODWT: length={length}"
        )));
    }
    let (lowpass, _) = wavelet_filters(wavelet)?;
    let limit = length / 2;
    let mut deepest = 1usize;
    while level_filter_width(deepest + 1, lowpass.len()) <= limit {
        deepest += 1;
    }
    Ok(deepest)
}

fn resolve_level(
    length: usize,
    level: Option<usize>,
    wavelet: &str,
    allow_overshoot: bool,
) -> Result<usize, SignalError> {
    let Some(level) = level else {
        return max_level(length, wavelet);
    };
    if level < 1 {
        return Err(SignalError::InvalidLevel(format!(
            "level must be >= 1, got {level}"
        )));
    }
    let (lowpass, _) = wavelet_filters(wavelet)?;
    if !allow_overshoot && level_filter_width(level, lowpass.len()) > length {
        return Err(SignalError::InvalidLevel(format!(
            "level {level} needs filter width {} > length {length}",
            level_filter_width(level, lowpass.len())
        )));
    }
    Ok(level)
}

fn step_correlate(series: &[f64], taps: &[f64], step: i64) -> Vec<f64> {
    let n = series.len();
    let mut output = vec![0.0f64; n];
    for (index, coefficient) in taps.iter().enumerate() {
        if *coefficient != 0.0 {
            let shift = step * index as i64;
            output = add_roll(&output, series, shift, *coefficient);
        }
    }
    output
}

fn add_roll(output: &[f64], series: &[f64], shift: i64, coefficient: f64) -> Vec<f64> {
    let n = series.len();
    let mut result = output.to_vec();
    for (t, slot) in result.iter_mut().enumerate() {
        let source = (t as i64 - shift).rem_euclid(n as i64) as usize;
        *slot += coefficient * series[source];
    }
    result
}

/// Compute the MODWT pyramid for a one-dimensional series.
///
/// # Errors
///
/// Returns [`SignalError`] for an unknown wavelet, empty/non-finite input, or
/// an invalid level.
pub fn modwt(
    values: &[f64],
    wavelet: &str,
    level: Option<usize>,
    boundary: Boundary,
) -> Result<ModwtResult, SignalError> {
    if values.is_empty() {
        return Err(SignalError::InvalidInput("MODWT input must be non-empty".to_string()));
    }
    if values.iter().any(|value| !value.is_finite()) {
        return Err(SignalError::InvalidInput("MODWT input must be finite".to_string()));
    }
    let length = values.len();
    let (lowpass, highpass) = wavelet_filters(wavelet)?;
    let resolved = resolve_level(
        length,
        level,
        wavelet,
        boundary == Boundary::Reflection,
    )?;
    let sqrt2 = std::f64::consts::SQRT_2;
    let scaled_low: Vec<f64> = lowpass.iter().map(|value| value / sqrt2).collect();
    let scaled_high: Vec<f64> = highpass.iter().map(|value| value / sqrt2).collect();
    let widths: Vec<usize> = (0..resolved)
        .map(|index| level_filter_width(index + 1, lowpass.len()) - 1)
        .collect();
    let pad = if boundary == Boundary::Reflection {
        widths[widths.len() - 1]
    } else {
        0
    };
    let mut working = if pad > 0 {
        reflect_pad(values, pad)
    } else {
        values.to_vec()
    };
    let mut details = Vec::with_capacity(resolved);
    for index in 0..resolved {
        let step = 1i64 << index;
        let detail = step_correlate(&working, &scaled_high, step);
        working = step_correlate(&working, &scaled_low, step);
        let detail = if pad > 0 {
            detail[pad..pad + length].to_vec()
        } else {
            detail
        };
        details.push(detail);
    }
    let scaling = if pad > 0 {
        working[pad..pad + length].to_vec()
    } else {
        working
    };
    Ok(ModwtResult {
        details,
        scaling,
        wavelet: wavelet.to_string(),
        boundary,
        boundary_widths: widths,
        length,
    })
}

/// Reflect-pad a series like `numpy.pad(..., mode="reflect")`.
#[must_use]
pub fn reflect_pad(values: &[f64], pad: usize) -> Vec<f64> {
    let n = values.len();
    if pad == 0 || n == 0 {
        return values.to_vec();
    }
    if n == 1 {
        return vec![values[0]; pad * 2 + 1];
    }
    let period = 2 * (n - 1);
    let mut output = vec![0.0f64; n + 2 * pad];
    for (position, slot) in output.iter_mut().enumerate() {
        let mut index = position as i64 - pad as i64;
        index = index.rem_euclid(period as i64);
        if index >= n as i64 {
            index = period as i64 - index;
        }
        *slot = values[index as usize];
    }
    output
}

fn synthesis_step(
    detail: &[f64],
    scaling: &[f64],
    scaled_low: &[f64],
    scaled_high: &[f64],
    step: i64,
) -> Vec<f64> {
    let mut coarser = step_correlate(detail, scaled_high, -step);
    let low = step_correlate(scaling, scaled_low, -step);
    for (slot, value) in coarser.iter_mut().zip(low.iter()) {
        *slot += value;
    }
    coarser
}

/// Reconstruct the series from MODWT coefficients.
///
/// # Errors
///
/// Returns [`SignalError::UnknownWavelet`] when the result carries an unknown
/// wavelet name.
pub fn imodwt(result: &ModwtResult) -> Result<Vec<f64>, SignalError> {
    let (lowpass, highpass) = wavelet_filters(&result.wavelet)?;
    let sqrt2 = std::f64::consts::SQRT_2;
    let scaled_low: Vec<f64> = lowpass.iter().map(|value| value / sqrt2).collect();
    let scaled_high: Vec<f64> = highpass.iter().map(|value| value / sqrt2).collect();
    let mut scaling = result.scaling.clone();
    for index in (0..result.details.len()).rev() {
        let step = 1i64 << index;
        scaling = synthesis_step(
            &result.details[index],
            &scaling,
            &scaled_low,
            &scaled_high,
            step,
        );
    }
    Ok(scaling)
}

fn detail_contribution(
    detail: &[f64],
    scaled_low: &[f64],
    scaled_high: &[f64],
    level_index: usize,
) -> Vec<f64> {
    let mut series = step_correlate(detail, scaled_high, -(1i64 << level_index));
    for lower in (0..level_index).rev() {
        series = step_correlate(&series, scaled_low, -(1i64 << lower));
    }
    series
}

/// Build the additive MRA from MODWT coefficients.
///
/// # Errors
///
/// Returns [`SignalError::UnknownWavelet`] when the result carries an unknown
/// wavelet name.
pub fn mra_from_modwt(result: &ModwtResult) -> Result<MraResult, SignalError> {
    let (lowpass, highpass) = wavelet_filters(&result.wavelet)?;
    let sqrt2 = std::f64::consts::SQRT_2;
    let scaled_low: Vec<f64> = lowpass.iter().map(|value| value / sqrt2).collect();
    let scaled_high: Vec<f64> = highpass.iter().map(|value| value / sqrt2).collect();
    let details = result
        .details
        .iter()
        .enumerate()
        .map(|(index, detail)| detail_contribution(detail, &scaled_low, &scaled_high, index))
        .collect();
    let mut smooth = result.scaling.clone();
    for lower in (0..result.details.len()).rev() {
        smooth = step_correlate(&smooth, &scaled_low, -(1i64 << lower));
    }
    Ok(MraResult {
        details,
        smooth,
        wavelet: result.wavelet.clone(),
        boundary: result.boundary,
        boundary_widths: result.boundary_widths.clone(),
        length: result.length,
    })
}

/// One-call additive multiresolution analysis.
///
/// # Errors
///
/// Returns [`SignalError`] under the same conditions as [`modwt`].
pub fn modwt_mra(
    values: &[f64],
    wavelet: &str,
    level: Option<usize>,
    boundary: Boundary,
) -> Result<MraResult, SignalError> {
    if values.is_empty() || values.iter().any(|value| !value.is_finite()) {
        return Err(SignalError::InvalidInput(
            "MRA input must be a finite non-empty series".to_string(),
        ));
    }
    let length = values.len();
    let (lowpass, _) = wavelet_filters(wavelet)?;
    let resolved = resolve_level(
        length,
        level,
        wavelet,
        boundary == Boundary::Reflection,
    )?;
    let pad = if boundary == Boundary::Reflection {
        level_filter_width(resolved, lowpass.len()) - 1
    } else {
        0
    };
    let working = if pad > 0 {
        reflect_pad(values, pad)
    } else {
        values.to_vec()
    };
    let coefficients = modwt(&working, wavelet, Some(resolved), Boundary::Circular)?;
    let mra = mra_from_modwt(&coefficients)?;
    let (details, smooth) = if pad > 0 {
        (
            mra.details
                .iter()
                .map(|detail| detail[pad..pad + length].to_vec())
                .collect(),
            mra.smooth[pad..pad + length].to_vec(),
        )
    } else {
        (mra.details, mra.smooth)
    };
    let widths: Vec<usize> = (0..resolved)
        .map(|index| level_filter_width(index + 1, lowpass.len()) - 1)
        .collect();
    Ok(MraResult {
        details,
        smooth,
        wavelet: wavelet.to_string(),
        boundary,
        boundary_widths: widths,
        length,
    })
}

/// Boolean mask of samples outside a level's leading boundary zone.
#[must_use]
pub fn valid_mask(width: usize, boundary_width: usize) -> Vec<bool> {
    (0..width).map(|index| index >= boundary_width).collect()
}

/// Unbiased wavelet-variance estimates from a MODWT.
#[must_use]
pub fn wavelet_variance(result: &ModwtResult) -> WaveletVariance {
    let mut variance = vec![f64::NAN; result.details.len()];
    let mut counts = vec![0usize; result.details.len()];
    for (index, detail) in result.details.iter().enumerate() {
        let mask = valid_mask(result.length, result.boundary_widths[index]);
        let samples: Vec<f64> = detail
            .iter()
            .zip(mask.iter())
            .filter_map(|(value, valid)| valid.then_some(*value))
            .collect();
        counts[index] = samples.len();
        if samples.is_empty() {
            continue;
        }
        let mean = samples.iter().sum::<f64>() / samples.len() as f64;
        variance[index] = samples
            .iter()
            .map(|value| (value - mean) * (value - mean))
            .sum::<f64>()
            / samples.len() as f64;
    }
    let total: f64 = variance.iter().filter(|value| value.is_finite()).sum();
    let shares = if total > 0.0 {
        variance.iter().map(|value| value / total).collect()
    } else {
        vec![f64::NAN; variance.len()]
    };
    WaveletVariance {
        variance,
        counts,
        shares,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn highpass_is_quadrature_mirror() {
        let (low, high) = wavelet_filters("la8").expect("filters");
        assert_eq!(low.len(), 8);
        for index in 0..8 {
            let sign = if index % 2 == 0 { 1.0 } else { -1.0 };
            assert!((high[index] - sign * low[7 - index]).abs() < 1e-15);
        }
    }

    #[test]
    fn filter_widths_match_the_closed_form() {
        assert_eq!(level_filter_width(1, 8), 8);
        assert_eq!(level_filter_width(2, 8), 22);
        assert_eq!(level_filter_width(3, 8), 50);
        assert_eq!(max_level(64, "la8").expect("max"), 2);
    }

    #[test]
    fn circular_modwt_is_exactly_invertible() {
        let values: Vec<f64> = (0..32)
            .map(|index| ((index as f64) * 0.7).sin() + ((index as f64) * 0.2).cos())
            .collect();
        let result = modwt(&values, "la8", Some(2), Boundary::Circular).expect("modwt");
        assert_eq!(result.details.len(), 2);
        assert_eq!(result.scaling.len(), values.len());
        let restored = imodwt(&result).expect("imodwt");
        for (a, b) in restored.iter().zip(values.iter()) {
            assert!((a - b).abs() < 1e-9, "inverse mismatch: {a} vs {b}");
        }
    }

    #[test]
    fn circular_mra_sums_to_input() {
        let values: Vec<f64> = (0..32)
            .map(|index| ((index as f64) * 0.7).sin() + ((index as f64) * 0.2).cos())
            .collect();
        let mra = modwt_mra(&values, "db4", Some(2), Boundary::Circular).expect("mra");
        let mut sum = mra.smooth.clone();
        for detail in &mra.details {
            for (slot, value) in sum.iter_mut().zip(detail.iter()) {
                *slot += value;
            }
        }
        for (a, b) in sum.iter().zip(values.iter()) {
            assert!((a - b).abs() < 1e-9);
        }
    }

    #[test]
    fn reflection_pad_matches_numpy() {
        let padded = reflect_pad(&[1.0, 2.0, 3.0], 2);
        assert_eq!(padded, vec![3.0, 2.0, 1.0, 2.0, 3.0, 2.0, 1.0]);
    }

    #[test]
    fn variance_excludes_the_boundary_zone() {
        let values: Vec<f64> = (0..32).map(|index| (index as f64).sin()).collect();
        let result = modwt(&values, "la8", Some(2), Boundary::Reflection).expect("modwt");
        let variance = wavelet_variance(&result);
        assert_eq!(variance.counts.len(), 2);
        assert_eq!(variance.counts[0], result.length - result.boundary_widths[0]);
        assert!(variance.variance[0].is_finite());
    }

    #[test]
    fn unknown_wavelet_is_rejected() {
        assert_eq!(
            wavelet_filters("nope"),
            Err(SignalError::UnknownWavelet("nope".to_string()))
        );
    }
}
