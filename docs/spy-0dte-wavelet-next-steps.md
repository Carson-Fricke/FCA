# SPY 0DTE Wavelet Studies — Findings and Next Steps

Status note for the wavelet signal-kernel wave (`harness.signal`, intraday and
daily studies) recorded at task completion. Findings are from the 1,527-session
bar cache (2020-08-10 through 2026-09-08) with the surrogate-gated pipeline in
`scripts/experiments/spy_0dte/wavelet_intraday.py`, `wavelet_daily.py`, and
`cross_timeframe.py`.

## Established findings

1. **Intraday shape archetypes do not beat the nulls.** HDBSCAN over the
   38-feature multi-scale shape space finds zero clusters (DBCV 0.000, all
   1,515 days labeled noise), identical to within-day iid shuffles,
   phase-randomized returns, and GBM-with-profile surrogates run through the
   identical pipeline. By construction, phase-randomized surrogates preserve
   the wavelet-variance fingerprint, so the pooled test cannot detect
   time-of-day phase organization — the conditional test (next steps #2) is
   required before declaring the phase hypothesis dead.
2. **Volatility regime is the dominant repeatable day-type structure.** Day
   realized vol alone clusters at DBCV 0.16 with 6 clusters and 176/1,515
   noise days — roughly 1000x the shape-space validity — while adding shape
   features to the vol feature destroys the clustering. Corollary: the
   apparent clusters in `price_clusters.py` raw-path clustering are largely
   vol-regime stratification, not path shape.
3. **A regular ~21-day realized-vol cycle exists at the daily scale.** The D4
   (16-32 day) detail of log realized vol advances at 0.299 rad/day
   (period ~= 21 days) with 0.99 phase-increment coherence; D5 (32-64 day)
   runs 0.142 rad/day at 0.99 coherence. The latest envelope sits at the 63rd
   (D4) / 92nd (D5) percentile.
4. **Daily regimes manifest intraday (descriptively).** High-vol-regime days
   carry +3.8pp more afternoon D3 energy, -4.3pp midday D4 energy, and smooth
   peaks ~20 minutes later in the session than low-vol-regime days. Price-trend
   regime differences are weaker than vol-regime differences.
5. **Boundary physics binds feature design at 1-minute bars.** The causal
   reflection analysis contaminates the first `L_j - 1` samples of each level,
   so D3/D4 block energy at the open block is structurally unmeasurable (the
   live-block gate drops it). Any "what happens at the open" claim needs the
   Morlet CWT local-energy route or shorter filters.

## Next steps (priority order)

1. **Vol-cycle predictability study.** Test whether daily vol-cycle phase
   (from the D4/D5 analytic phase of log realized vol) predicts forward
   realized vol at 1/5/21-day horizons. Benchmarks: trailing RV, EWMA;
   walk-forward splits; surrogate discipline identical to the intraday study.
   The 0.99-coherence cycle is the one strong periodic signal found; if it
   predicts forward vol, it is actionable at the daily timeframe.
2. **Conditional phase test within vol strata.** Rerun the intraday phase
   features (D3/D4 block phase velocity and coherence) within day-vol
   quintiles, real vs phase-randomized surrogates per stratum. This is the
   sharp test of the trend-vs-mean-reversion time-of-day hypothesis that the
   pooled test could not separate.
3. **Condition the 0DTE theta/IV program on regime.** Join
   `analyze.py` / `intraday_profile.py` outputs (theta multipliers, decay
   share, IV path drops) with the daily regime table via
   `cross_timeframe.py`'s `zero_dte_overlay.parquet`. Question: is decay
   front-loading or the excess-clock effect conditional on vol-cycle phase or
   trend state? This is the directly tradable bridge to the H8.5 program.
4. **Open-window local analysis.** Measure Morlet CWT local energy at minutes
   0-30 (where MODWT D3/D4 block features are boundary-dead) with reflection
   semantics and surrogate controls, answering the original time-of-day
   hypothesis at the open where 0DTE action concentrates.
5. **Study hardening.** Bump surrogate reps (12 -> 50) for a publish-grade
   null verdict, record finding #1 in a durable research note to avoid
   re-litigating it, and wire the scalogram artifact into the thesis UI if
   the conditional tests surface structure worth visualizing.

## Artifacts

- `src/harness/signal/` — MODWT/MRA/variance, analytic signal, Morlet CWT kernels
- `.local/spy-clusters/wavelet/` — features.parquet, clusters.csv, scalogram.parquet, summary.json
- `.local/spy-clusters/wavelet-daily/` — daily-variance.csv, daily-mra.parquet, rolling-variance.csv, summary.json
- `.local/spy-clusters/cross-timeframe/` — cross_timeframe.json, zero_dte_overlay.parquet
