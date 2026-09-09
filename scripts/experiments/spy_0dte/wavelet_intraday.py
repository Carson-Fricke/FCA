"""Wavelet-based intraday pattern clustering for SPY 1-minute bars.

For every cached session of 1-minute SPY closes this study:

1. Builds 1-minute log-returns (never price levels) and vol-normalizes each
   day by the cross-day median U-shape minute-vol profile times a day-level
   scalar, removing the deterministic intraday vol pattern while keeping
   pure day-shape information.
2. Decomposes each day with the reflection-boundary MODWT (LA8, 4 levels on
   390 minutes: D1 2-4 min, D2 4-8, D3 8-16, D4 16-32, S4 32+).
3. Extracts multi-scale, time-of-day-anchored features: per-level energy
   shares, block energy concentrations (open/morning/midday/afternoon/power
   matching analyze.py buckets), block phase velocity and per-scale phase
   coherence from the analytic signal of the D3/D4 details (trending vs
   mean-reverting at each scale), envelope burstiness, and smooth-shape
   descriptors.
4. Clusters days with HDBSCAN over standardized PCA-reduced features and
   compares cluster validity (DBCV) against three surrogate families run
   through the identical pipeline (same vol-profile normalization, same
   features, same clustering): within-day iid shuffles, phase-randomized
   returns (preserve the amplitude spectrum hence the wavelet variance
   fingerprint), and GBM days with the fitted vol profile. Validity above
   the phase-randomized null is attributable to time-of-day phase
   organization specifically.
5. Writes a Morlet CWT scalogram artifact: per-cluster median time-frequency
   power (exploratory only, never feature-bearing).

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/wavelet_intraday.py `
        --bar-cache .local/spy-clusters/bar-cache --out .local/spy-clusters/wavelet
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import hdbscan
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from analyze import _TOD_EDGES
from sklearn.decomposition import PCA
from sklearn.metrics import adjusted_rand_score
from sklearn.preprocessing import StandardScaler

from harness.signal.analytic import (
    analytic_signal,
    block_phase_features,
    phase_coherence,
)
from harness.signal.scalogram import log_scales, morlet_periods, scalogram
from harness.signal.wavelets import modwt, modwt_mra, valid_mask, wavelet_variance

_LEVELS = 4
_MIN_BARS = 360
_GRID = 390
_PROFILE_FLOOR = 1e-6
_ENERGY_LEVELS = (2, 3, 4)
_PHASE_LEVELS = (3, 4)
_MIN_BLOCK_CLEAN = 10
_CWT_MIN_PERIOD = 2.0
_CWT_MAX_PERIOD = 96.0
_CWT_VOICES = 12
_OVERALL_LABEL = -999
_SURROGANTE_FAMILIES = ("iid", "phase", "gbm")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bar-cache", type=Path, default=Path(".local/spy-clusters/bar-cache")
    )
    parser.add_argument("--out", type=Path, default=Path(".local/spy-clusters/wavelet"))
    parser.add_argument("--wavelet", type=str, default="la8")
    parser.add_argument("--levels", type=int, default=_LEVELS)
    parser.add_argument("--min-cluster-size", type=int, default=25)
    parser.add_argument("--min-samples", type=int, default=None)
    parser.add_argument("--stability-iterations", type=int, default=30)
    parser.add_argument("--surrogate-reps", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--no-scalogram", action="store_true")
    parser.add_argument("--no-surrogates", action="store_true")
    args = parser.parse_args(argv)
    args.bar_cache = args.bar_cache.resolve()
    args.out = args.out.resolve()
    return args


def load_bar_cache(cache_dir: Path, max_sessions: int | None) -> dict[str, np.ndarray]:
    """Load cached 1-min close lists keyed by ISO session date."""
    sessions: dict[str, np.ndarray] = {}
    paths = sorted(cache_dir.glob("*.json"))
    if max_sessions is not None:
        paths = paths[:max_sessions]
    for path in paths:
        closes = np.asarray(json.loads(path.read_text(encoding="utf-8")), dtype=float)
        sessions[path.stem] = closes
    print(f"loaded {len(sessions)} cached sessions from {cache_dir}", flush=True)
    return sessions


def session_returns(closes: np.ndarray) -> np.ndarray | None:
    """390-minute return grid for one session, or None if too short."""
    usable = closes[closes > 0.0]
    if usable.size < _MIN_BARS:
        return None
    held = np.empty(_GRID, dtype=float)
    take = min(usable.size, _GRID)
    held[:take] = usable[:take]
    if take < _GRID:
        held[take:] = usable[take - 1]
    log_close = np.log(held)
    returns = np.zeros(_GRID, dtype=float)
    returns[1:] = np.diff(log_close)
    return returns


def fit_vol_profile(
    returns: dict[str, np.ndarray],
) -> tuple[np.ndarray, dict[str, float]]:
    """Cross-day median U-shape profile and per-day vol scalars."""
    keys = sorted(returns)
    matrix = np.array([returns[key] for key in keys], dtype=float)
    profile = np.maximum(np.median(np.abs(matrix), axis=0), _PROFILE_FLOOR)
    scalars: dict[str, float] = {}
    for index, key in enumerate(keys):
        ratio = np.abs(matrix[index]) / profile
        scalars[key] = float(max(np.median(ratio), _PROFILE_FLOOR))
    return profile, scalars


def day_vol_raw(scalar: float, profile: np.ndarray) -> float:
    """Day's median absolute 1-min return in raw units."""
    return float(scalar * float(np.median(profile)))


def _block_bounds() -> list[tuple[int, int, str]]:
    return [(start, stop, name) for start, stop, name in _TOD_EDGES]


def _live_blocks(boundary_width: int) -> list[tuple[int, int, str]]:
    """Blocks with at least _MIN_BLOCK_CLEAN clean samples at this level.

    The causal reflection analysis contaminates the first
    ``boundary_width`` samples of each level, so blocks entirely or mostly
    inside that zone carry no clean data (e.g. the open block at D3/D4).
    """
    live = []
    for start, stop, name in _block_bounds():
        clean_start = max(start, boundary_width)
        if stop - clean_start >= _MIN_BLOCK_CLEAN:
            live.append((start, stop, name))
    return live


def day_features(
    day_returns: np.ndarray,
    profile: np.ndarray,
    scalar: float,
    wavelet: str,
    levels: int,
) -> dict[str, float]:
    """Multi-scale time-of-day features for one day of raw returns."""
    normalized = day_returns / (scalar * profile)
    coefficients = modwt(
        normalized, wavelet=wavelet, level=levels, boundary="reflection"
    )
    mra = modwt_mra(normalized, wavelet=wavelet, level=levels, boundary="reflection")
    variance = wavelet_variance(coefficients)

    features: dict[str, float] = {}
    components = [variance.variance[level] for level in range(levels)]
    smooth_mask = valid_mask(_GRID, coefficients.boundary_widths[-1])
    smooth_values = coefficients.scaling[smooth_mask]
    smooth_energy = float(np.mean(np.square(smooth_values - smooth_values.mean())))
    components.append(smooth_energy)
    total = float(np.sum(components))
    for level in range(levels):
        features[f"energy_d{level + 1}"] = float(variance.variance[level] / total)
    features["energy_smooth"] = smooth_energy / total

    for level in _ENERGY_LEVELS:
        detail = coefficients.details[level - 1]
        mask = valid_mask(_GRID, coefficients.boundary_widths[level - 1])
        level_energy = float(np.sum(np.square(detail[mask])))
        for start, stop, name in _live_blocks(coefficients.boundary_widths[level - 1]):
            block_mask = mask.copy()
            block_mask[:start] = False
            block_mask[stop:] = False
            share = (
                float(np.sum(np.square(detail[block_mask]))) / level_energy
                if level_energy > 0.0
                else np.nan
            )
            features[f"block_{name}_d{level}"] = share

    smooth_coefficients = coefficients.scaling
    smooth_width = coefficients.boundary_widths[-1]
    smooth_mask = valid_mask(_GRID, smooth_width)
    smooth_energy = float(np.sum(np.square(smooth_coefficients[smooth_mask])))
    for start, stop, name in _live_blocks(smooth_width):
        block_mask = smooth_mask.copy()
        block_mask[:start] = False
        block_mask[stop:] = False
        share = (
            float(np.sum(np.square(smooth_coefficients[block_mask]))) / smooth_energy
            if smooth_energy > 0.0
            else np.nan
        )
        features[f"block_{name}_smooth"] = share

    for level in _PHASE_LEVELS:
        detail = mra.details[level - 1]
        mask = valid_mask(_GRID, mra.boundary_widths[level - 1])
        envelope, phase = analytic_signal(detail)
        live = _live_blocks(mra.boundary_widths[level - 1])
        speeds, _coherences = block_phase_features(phase, [(b[0], b[1]) for b in live])
        for position, (_start, _stop, name) in enumerate(live):
            features[f"phase_speed_{name}_d{level}"] = float(speeds[position])
        features[f"phase_coherence_d{level}"] = (
            phase_coherence(phase[mask]) if mask.any() else np.nan
        )
    for level in _ENERGY_LEVELS:
        detail = mra.details[level - 1]
        mask = valid_mask(_GRID, mra.boundary_widths[level - 1])
        envelope, _phase = analytic_signal(detail)
        values = envelope[mask]
        features[f"envelope_cv_d{level}"] = (
            float(values.std() / values.mean()) if values.mean() > 0.0 else np.nan
        )

    smooth = mra.smooth
    path = np.diff(smooth)
    net = float(smooth[-1] - smooth[0])
    total_variation = float(np.sum(np.abs(path)))
    features["smooth_net"] = net
    features["smooth_efficiency"] = (
        abs(net) / total_variation if total_variation > 0.0 else np.nan
    )
    features["smooth_argmax"] = float(np.argmax(smooth)) / _GRID
    features["smooth_argmin"] = float(np.argmin(smooth)) / _GRID
    return features


def cluster_features(
    features: pd.DataFrame, min_cluster_size: int, min_samples: int | None
) -> tuple[np.ndarray, np.ndarray, float, int]:
    """Standardize, PCA-reduce, HDBSCAN; return labels, probs, DBCV, count."""
    filled = features.fillna(features.median())
    scaler = StandardScaler()
    scaled = scaler.fit_transform(filled.to_numpy(dtype=float))
    pca = PCA(n_components=0.95, svd_solver="full")
    reduced = pca.fit_transform(scaled)
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        gen_min_span_tree=True,
    )
    labels = clusterer.fit_predict(reduced)
    validity = float(clusterer.relative_validity_)
    n_clusters = int(labels.max() + 1) if labels.max() >= 0 else 0
    return labels, clusterer.probabilities_, validity, n_clusters


def stability_ari(
    features: pd.DataFrame,
    full_labels: np.ndarray,
    min_cluster_size: int,
    min_samples: int | None,
    iterations: int,
    seed: int,
) -> float:
    """Bootstrap co-assignment stability: subsample vs full-day clustering."""
    rng = np.random.default_rng(seed)
    count = len(features)
    scores = []
    for _iteration in range(iterations):
        sample = rng.choice(count, size=int(count * 0.8), replace=False)
        labels, _prob, _validity, _n = cluster_features(
            features.iloc[sample], min_cluster_size, min_samples
        )
        scores.append(adjusted_rand_score(full_labels[sample], labels))
    return float(np.mean(scores)) if scores else float("nan")


def surrogate_matrix(
    matrix: np.ndarray,
    family: str,
    scalars: np.ndarray,
    profile: np.ndarray,
    seed: int,
) -> np.ndarray:
    """One surrogate dataset of raw returns for a null family."""
    rng = np.random.default_rng(seed)
    count, width = matrix.shape
    if family == "iid":
        surrogate = np.empty_like(matrix)
        for day in range(count):
            surrogate[day] = rng.permutation(matrix[day])
    elif family == "phase":
        assert width % 2 == 0
        spectrum = np.fft.fft(matrix, axis=1)
        factors = np.exp(
            1j * rng.uniform(0.0, 2.0 * np.pi, size=(count, width // 2 - 1))
        )
        spectrum[:, 1 : width // 2] *= factors
        spectrum[:, width // 2 + 1 :] = np.conj(spectrum[:, 1 : width // 2][:, ::-1])
        surrogate = np.fft.ifft(spectrum, axis=1).real
    elif family == "gbm":
        unit_median = float(np.median(np.abs(rng.standard_normal(100_000))))
        noise = rng.standard_normal(matrix.shape) / unit_median
        surrogate = scalars[:, None] * profile[None, :] * noise
    else:
        raise ValueError(f"unknown surrogate family {family!r}")
    return surrogate


def run_surrogate_study(
    matrix: np.ndarray,
    keys: list[str],
    scalars: dict[str, float],
    profile: np.ndarray,
    cfg: argparse.Namespace,
) -> dict[str, dict]:
    """Cluster validity of each surrogate family through the same pipeline."""
    scalar_values = np.array([scalars[key] for key in keys], dtype=float)
    results: dict[str, dict] = {}
    for family_index, family in enumerate(_SURROGANTE_FAMILIES):
        validities = []
        cluster_counts = []
        for rep in range(cfg.surrogate_reps):
            seed = cfg.seed + 10_000 * family_index + rep
            surrogate = surrogate_matrix(matrix, family, scalar_values, profile, seed)
            frame = pd.DataFrame(
                [
                    day_features(
                        surrogate[index],
                        profile,
                        scalars[keys[index]],
                        cfg.wavelet,
                        cfg.levels,
                    )
                    for index in range(surrogate.shape[0])
                ],
                index=keys,
            )
            _labels, _probabilities, validity, n_clusters = cluster_features(
                frame, cfg.min_cluster_size, cfg.min_samples
            )
            validities.append(validity)
            cluster_counts.append(n_clusters)
            if (rep + 1) % 4 == 0:
                print(
                    f"  surrogate {family} {rep + 1}/{cfg.surrogate_reps}: "
                    f"dbcv {validity:.4f}, clusters {n_clusters}",
                    flush=True,
                )
        results[family] = {
            "dbcv_mean": float(np.mean(validities)),
            "dbcv_std": float(np.std(validities)),
            "n_clusters_mean": float(np.mean(cluster_counts)),
            "reps": cfg.surrogate_reps,
        }
    return results


def scalogram_artifact(
    labels: np.ndarray,
    keys: list[str],
    returns: dict[str, np.ndarray],
    scalars: dict[str, float],
    profile: np.ndarray,
    wavelet_levels: int,
) -> pd.DataFrame:
    """Per-cluster median Morlet scalogram power (exploratory artifact)."""
    scales = log_scales(_CWT_MIN_PERIOD, _CWT_MAX_PERIOD, _CWT_VOICES)
    periods = morlet_periods(scales)
    per_label: dict[int, list[np.ndarray]] = {}
    overall: list[np.ndarray] = []
    for index, key in enumerate(keys):
        normalized = returns[key] / (scalars[key] * profile)
        power, mask = scalogram(normalized, scales)
        clean = power[:, mask.all(axis=0)]
        per_label.setdefault(int(labels[index]), []).append(clean)
        overall.append(clean)
        if (index + 1) % 250 == 0:
            print(f"  scalograms {index + 1}/{len(keys)}", flush=True)
    rows = []
    for label, powers in sorted(per_label.items()):
        median_power = np.median(np.array(powers), axis=0)
        for scale_index, period in enumerate(periods):
            for minute in range(median_power.shape[1]):
                rows.append(
                    {
                        "label": label,
                        "period_min": float(period),
                        "minute": minute,
                        "power": float(median_power[scale_index, minute]),
                        "days": len(powers),
                    }
                )
    median_overall = np.median(np.array(overall), axis=0)
    for scale_index, period in enumerate(periods):
        for minute in range(median_overall.shape[1]):
            rows.append(
                {
                    "label": _OVERALL_LABEL,
                    "period_min": float(period),
                    "minute": minute,
                    "power": float(median_overall[scale_index, minute]),
                    "days": len(overall),
                }
            )
    assert wavelet_levels > 0
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    sessions = load_bar_cache(cfg.bar_cache, cfg.max_sessions)
    returns: dict[str, np.ndarray] = {}
    for key, closes in sessions.items():
        grid_returns = session_returns(closes)
        if grid_returns is not None:
            returns[key] = grid_returns
    print(
        f"{len(returns)}/{len(sessions)} sessions with >= {_MIN_BARS} bars", flush=True
    )
    if len(returns) < 30:
        print("not enough sessions to cluster", flush=True)
        return 1
    profile, scalars = fit_vol_profile(returns)
    keys = sorted(returns)

    print(f"extracting features ({cfg.wavelet}, {cfg.levels} levels)...", flush=True)
    records = [
        day_features(returns[key], profile, scalars[key], cfg.wavelet, cfg.levels)
        for key in keys
    ]
    features = pd.DataFrame(records, index=keys)
    print(
        f"features: {features.shape[1]} columns x {features.shape[0]} days", flush=True
    )

    labels, probabilities, validity, n_clusters = cluster_features(
        features, cfg.min_cluster_size, cfg.min_samples
    )
    print(
        f"hdbscan: {n_clusters} clusters, noise {int((labels < 0).sum())} days, "
        f"DBCV {validity:.4f}",
        flush=True,
    )

    summary: dict = {
        "config": {
            "wavelet": cfg.wavelet,
            "levels": cfg.levels,
            "min_cluster_size": cfg.min_cluster_size,
            "sessions": len(returns),
            "features": int(features.shape[1]),
            "seed": cfg.seed,
        },
        "clustering": {
            "dbcv": validity,
            "n_clusters": n_clusters,
            "noise_days": int((labels < 0).sum()),
        },
    }
    if not cfg.no_surrogates:
        print("running surrogate nulls (identical pipeline)...", flush=True)
        surrogates = run_surrogate_study(
            np.array([returns[key] for key in keys], dtype=float),
            keys,
            scalars,
            profile,
            cfg,
        )
        for stats in surrogates.values():
            sigma = stats["dbcv_std"]
            stats["z_vs_real"] = (
                (validity - stats["dbcv_mean"]) / sigma
                if sigma > 1e-12
                else float("nan")
            )
            stats["real_dbcv"] = validity
        summary["surrogates"] = surrogates
        for family, stats in surrogates.items():
            print(
                f"  {family}: dbcv {stats['dbcv_mean']:.4f} +/- {stats['dbcv_std']:.4f}, "
                f"z vs real {stats['z_vs_real']:+.2f}",
                flush=True,
            )

    print("bootstrap stability (ARI)...", flush=True)
    ari = stability_ari(
        features,
        labels,
        cfg.min_cluster_size,
        cfg.min_samples,
        cfg.stability_iterations,
        cfg.seed,
    )
    summary["clustering"]["stability_ari"] = ari
    print(f"  mean ARI {ari:.4f}", flush=True)

    day_vols = {key: day_vol_raw(scalars[key], profile) for key in keys}
    vol_values = np.array([day_vols[key] for key in keys], dtype=float)
    vol_clusterer = hdbscan.HDBSCAN(
        min_cluster_size=cfg.min_cluster_size, gen_min_span_tree=True
    )
    vol_labels = vol_clusterer.fit_predict(vol_values[:, None])
    vol_control = {
        "dbcv": float(vol_clusterer.relative_validity_),
        "n_clusters": int(vol_labels.max() + 1) if vol_labels.max() >= 0 else 0,
        "noise_days": int((vol_labels < 0).sum()),
    }
    summary["vol_regime_control"] = vol_control
    print(
        f"positive control (day-vol only): {vol_control['n_clusters']} clusters, "
        f"DBCV {vol_control['dbcv']:.4f}",
        flush=True,
    )

    cfg.out.mkdir(parents=True, exist_ok=True)
    vol_quintiles = pd.qcut(vol_values, 5, labels=False, duplicates="drop")
    enriched = features.copy()
    enriched.insert(0, "session", keys)
    enriched["label"] = labels
    enriched["probability"] = probabilities
    enriched["day_vol_raw"] = [day_vols[key] for key in keys]
    enriched["vol_quintile"] = vol_quintiles
    pq.write_table(
        pa.Table.from_pandas(enriched, preserve_index=False),
        cfg.out / "features.parquet",
        compression="zstd",
    )

    cluster_rows = []
    drive_columns = [
        column
        for column in (
            f"block_{name}_d{level}"
            for level in _PHASE_LEVELS
            for _start, _stop, name in _block_bounds()
        )
        if column in enriched.columns
    ]
    for label in sorted(int(value) for value in set(labels.tolist())):
        mask = labels == label
        member_keys = [keys[index] for index in np.flatnonzero(mask)]
        row: dict = {
            "label": int(label),
            "days": int(mask.sum()),
            "mean_day_vol": float(np.mean([day_vols[key] for key in member_keys])),
            "mean_probability": float(np.mean(probabilities[mask])),
        }
        for column in drive_columns:
            row[f"mean_{column}"] = float(
                np.nanmean(enriched.loc[mask, column].to_numpy(dtype=float))
            )
        cluster_rows.append(row)
    clusters = pd.DataFrame(cluster_rows)
    clusters.to_csv(cfg.out / "clusters.csv", index=False)

    if not cfg.no_scalogram:
        print("writing scalogram artifact...", flush=True)
        artifact = scalogram_artifact(
            labels, keys, returns, scalars, profile, cfg.levels
        )
        pq.write_table(
            pa.Table.from_pandas(artifact, preserve_index=False),
            cfg.out / "scalogram.parquet",
            compression="zstd",
        )

    (cfg.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"\nwrote {cfg.out / 'summary.json'}", flush=True)
    print(clusters.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
