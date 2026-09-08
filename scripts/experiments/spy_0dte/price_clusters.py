"""SPY intraday price-path clustering study (6-year dataset).

Downloads 6 years of SPY 1-minute bars from Alpaca, normalizes each session
to a percentage-change-from-open price path, clusters them using k-Shape
(shape-based distance via normalized cross-correlation), and computes
per-cluster per-minute statistics (% positive, % negative, mean return).

Writes a JSON fixture consumed by the thesis UI clustering study page:

    src/ui/src/lib/data/spy0dteClusters.js

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/price_clusters.py `
        --data-root .local/spy-clusters `
        --out src/ui/src/lib/data/spy0dteClusters.js `
        --years 6 --clusters 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
from osi import closed_sessions

from lake.configuration import load_settings
from lake.providers.alpaca.client import AlpacaClient

# ---------------------------------------------------------------------------
# Data download
# ---------------------------------------------------------------------------


async def download_bars(
    client: AlpacaClient,
    sessions: list[tuple[date, datetime, datetime]],
    cache_dir: Path,
) -> dict[str, list[float]]:
    """Download 1-minute SPY bars per session, caching to disk."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, list[float]] = {}
    total = len(sessions)
    for index, (session_date, open_time, close_time) in enumerate(sessions):
        key = session_date.isoformat()
        cache_file = cache_dir / f"{key}.json"
        if cache_file.is_file():
            result[key] = json.loads(cache_file.read_text(encoding="utf-8"))
            continue
        params: dict[str, str | int] = {
            "symbols": "SPY",
            "start": open_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": close_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeframe": "1Min",
            "limit": 10_000,
        }
        closes: list[float] = []
        page_token: str | None = None
        while True:
            if page_token:
                params["page_token"] = page_token
            response = await client._request("/v2/stocks/bars", params)
            payload = response.json()
            bars = payload.get("bars", {}).get("SPY", [])
            for bar in bars:
                closes.append(float(bar.get("c", 0)))
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        if closes:
            cache_file.write_text(json.dumps(closes), encoding="utf-8")
            result[key] = closes
        if (index + 1) % 50 == 0:
            print(f"  downloaded {index + 1}/{total} sessions", flush=True)
    print(f"downloaded {len(result)}/{total} sessions of SPY 1-min bars", flush=True)
    return result


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_paths(
    raw_bars: dict[str, list[float]], target_length: int = 390
) -> tuple[np.ndarray, list[str]]:
    """Normalize each session to percentage change from open, padded to target length."""
    keys = sorted(raw_bars)
    paths: list[np.ndarray] = []
    valid_keys: list[str] = []
    for key in keys:
        closes = np.array(raw_bars[key], dtype=float)
        closes = closes[closes > 0]
        if len(closes) < 60:
            continue
        open_price = closes[0]
        if open_price <= 0:
            continue
        pct_change = (closes / open_price - 1.0) * 100.0
        if len(pct_change) >= target_length:
            path = pct_change[:target_length]
        else:
            path = np.pad(pct_change, (0, target_length - len(pct_change)), mode="edge")
        paths.append(path)
        valid_keys.append(key)
    matrix = np.array(paths, dtype=np.float64)
    return matrix, valid_keys


# ---------------------------------------------------------------------------
# k-Shape clustering (SBD distance + normalized centroid)
# ---------------------------------------------------------------------------


def _nccc(x: np.ndarray, y: np.ndarray) -> float:
    """Normalized cross-correlation between two sequences."""
    x_norm = np.linalg.norm(x)
    y_norm = np.linalg.norm(y)
    if x_norm < 1e-10 or y_norm < 1e-10:
        return 0.0
    n = len(x)
    fft_len = 2 * n
    fft_x = np.fft.rfft(x, fft_len)
    fft_y = np.fft.rfft(y[::-1], fft_len)
    corr = np.fft.irfft(fft_x * fft_y, fft_len)
    corr = corr[n - 1 :]
    return float(np.max(corr) / (x_norm * y_norm))


def sbd(x: np.ndarray, y: np.ndarray) -> float:
    """Shape-based distance: 1 - max NCC, invariant to shift and scale."""
    return 1.0 - _nccc(x, y)


def _zscore(path: np.ndarray) -> np.ndarray:
    std = np.std(path)
    if std < 1e-10:
        return np.zeros_like(path)
    return (path - np.mean(path)) / std


def kshape(
    paths: np.ndarray, k: int, max_iter: int = 100, seed: int = 42
) -> tuple[np.ndarray, np.ndarray]:
    """k-Shape clustering. Returns (labels, centroids)."""
    rng = np.random.default_rng(seed)
    n = len(paths)
    zpaths = np.array([_zscore(p) for p in paths])
    indices = rng.permutation(n)[:k]
    centroids = np.array([zpaths[i] for i in indices])

    labels = np.full(n, -1, dtype=int)
    for iteration in range(max_iter):
        new_labels = np.full(n, -1, dtype=int)
        for i in range(n):
            distances = [sbd(zpaths[i], centroids[j]) for j in range(k)]
            new_labels[i] = int(np.argmin(distances))
        if np.array_equal(new_labels, labels) and iteration > 0:
            break
        labels = new_labels
        for j in range(k):
            members = np.where(labels == j)[0]
            if len(members) == 0:
                continue
            centroids[j] = _shape_extraction(zpaths[members])
    return labels, centroids


def _shape_extraction(members: np.ndarray) -> np.ndarray:
    """Compute the k-Shape centroid via cross-correlation alignment."""
    m = len(members)
    if m == 1:
        return members[0].copy()
    n = len(members[0])
    fft_len = 2 * n

    ref = members[0]
    ref_norm = np.linalg.norm(ref)
    if ref_norm < 1e-10:
        ref_norm = 1.0

    # Sum normalized cross-correlation across all members at each shift
    sum_ncc = np.zeros(n)
    for i in range(m):
        y = members[i]
        y_norm = np.linalg.norm(y)
        if y_norm < 1e-10:
            continue
        corr_full = np.fft.irfft(
            np.fft.rfft(ref, fft_len) * np.fft.rfft(y[::-1], fft_len),
            fft_len,
        )
        sum_ncc += corr_full[n - 1 : n - 1 + n] / (ref_norm * y_norm)

    best_shift = int(np.argmax(sum_ncc))
    aligned = np.array([np.roll(m, best_shift) for m in members])
    centroid = np.mean(aligned, axis=0)
    return _zscore(centroid)


# ---------------------------------------------------------------------------
# Per-cluster statistics
# ---------------------------------------------------------------------------


def cluster_stats(
    paths: np.ndarray, labels: np.ndarray, keys: list[str], k: int
) -> list[dict]:
    """Per-cluster statistics: centroid, % positive per minute, count, avg return."""
    clusters: list[dict] = []
    for j in range(k):
        members = np.where(labels == j)[0]
        if len(members) == 0:
            continue
        member_paths = paths[members]
        centroid = np.mean(member_paths, axis=0)
        pct_positive = np.mean(member_paths > 0, axis=0) * 100
        pct_negative = np.mean(member_paths < 0, axis=0) * 100
        avg_return = float(np.mean(member_paths[:, -1]))
        clusters.append(
            {
                "cluster": j,
                "count": int(len(members)),
                "avgReturn": round(avg_return, 4),
                "sessions": [keys[i] for i in members],
                "centroid": [round(float(v), 4) for v in centroid],
                "pctPositive": [round(float(v), 2) for v in pct_positive],
                "pctNegative": [round(float(v), 2) for v in pct_negative],
            }
        )
    clusters.sort(key=lambda c: -c["count"])
    return clusters


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--years", type=int, default=6)
    parser.add_argument("--clusters", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.out = args.out.resolve()
    return args


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if settings.provider.api_key is None or settings.provider.api_secret is None:
        print(
            "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET required", flush=True
        )
        return 2

    today = datetime.now(UTC).date()
    lookback_start = today - timedelta(days=args.years * 365 + 30)
    sessions = closed_sessions(lookback_start, today)
    print(f"found {len(sessions)} closed sessions over {args.years} years", flush=True)

    client = AlpacaClient.from_settings(settings.provider)
    try:
        raw_bars = await download_bars(client, sessions, args.data_root / "bar-cache")
    finally:
        await client.aclose()

    paths, keys = normalize_paths(raw_bars, target_length=390)
    print(f"normalized {len(paths)} price paths (390 minutes each)", flush=True)

    k = args.clusters
    labels, centroids = kshape(paths, k, seed=args.seed)
    print(f"k-Shape clustering complete: k={k}", flush=True)

    cluster_data = cluster_stats(paths, labels, keys, k)
    print(f"computed stats for {len(cluster_data)} non-empty clusters", flush=True)

    # Per-minute overall stats (all sessions pooled)
    pct_pos_overall = np.mean(paths > 0, axis=0) * 100
    pct_neg_overall = np.mean(paths < 0, axis=0) * 100

    fixture = {
        "meta": {
            "sessions": len(paths),
            "firstSession": keys[0] if keys else "",
            "lastSession": keys[-1] if keys else "",
            "clusters": k,
            "years": args.years,
            "normalized": "percentage change from open, padded to 390 minutes",
            "algorithm": "k-Shape (shape-based distance via normalized cross-correlation)",
        },
        "clusters": cluster_data,
        "overall": {
            "pctPositive": [round(float(v), 2) for v in pct_pos_overall],
            "pctNegative": [round(float(v), 2) for v in pct_neg_overall],
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "/* Generated by scripts/experiments/spy_0dte/price_clusters.py - do not edit. */\n"
        "export const clusters = " + json.dumps(fixture, indent=1) + ";\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}", flush=True)
    for cluster in cluster_data:
        print(
            f"  cluster {cluster['cluster']}: {cluster['count']} sessions, "
            f"avg return {cluster['avgReturn']:+.2f}%",
            flush=True,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
