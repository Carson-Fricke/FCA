//! Built-in signal kernels: MODWT, analytic signal, and Morlet scalograms.
//!
//! Port of `harness.signal`. All kernels are pure, synchronous float64
//! transforms with no wall-clock, network, or unseeded-random input.

pub mod analytic;
pub mod fft;
pub mod scalogram;
pub mod wavelets;

pub use analytic::{
    analytic_signal, block_phase_features, instantaneous_phase_velocity, phase_coherence, unwrap,
};
pub use scalogram::{
    cone_mask, log_scales, morlet_cwt, morlet_periods, scalogram, Complex, DEFAULT_OMEGA0,
};
pub use wavelets::{
    imodwt, level_filter_width, max_level, modwt, modwt_mra, mra_from_modwt, reflect_pad,
    valid_mask, wavelet_filters, wavelet_names, wavelet_variance, Boundary, ModwtResult, MraResult,
    SignalError, WaveletVariance, DB4_LOWPASS, LA8_LOWPASS,
};
