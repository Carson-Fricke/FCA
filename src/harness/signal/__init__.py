"""Public signal-processing facade for the quant-harness.

Time-series kernels used by market-microstructure and intraday-pattern
experiments: the maximal-overlap discrete wavelet transform (MODWT) with
additive MRA and wavelet-variance estimation, analytic-signal (Hilbert)
envelope and phase features for band-limited details, and an exploratory
Morlet CWT scalogram. See ``.github/architecture/repository.md`` for the
package boundary.
"""

from harness.signal.analytic import (
    analytic_signal,
    block_phase_features,
    instantaneous_phase_velocity,
    phase_coherence,
)
from harness.signal.scalogram import (
    DEFAULT_OMEGA0,
    log_scales,
    morlet_cwt,
    morlet_periods,
    scalogram,
)
from harness.signal.wavelets import (
    DB4_LOWPASS,
    LA8_LOWPASS,
    ModwtResult,
    MraResult,
    WaveletVariance,
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

__all__ = [
    "DB4_LOWPASS",
    "DEFAULT_OMEGA0",
    "LA8_LOWPASS",
    "ModwtResult",
    "MraResult",
    "WaveletVariance",
    "analytic_signal",
    "block_phase_features",
    "imodwt",
    "instantaneous_phase_velocity",
    "level_filter_width",
    "log_scales",
    "max_level",
    "modwt",
    "modwt_mra",
    "mra_from_modwt",
    "morlet_cwt",
    "morlet_periods",
    "phase_coherence",
    "scalogram",
    "valid_mask",
    "wavelet_filters",
    "wavelet_names",
    "wavelet_variance",
]
