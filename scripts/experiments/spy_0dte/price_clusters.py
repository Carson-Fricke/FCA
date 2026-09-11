"""SPY intraday price-path clustering study (6-year dataset).

Downloads 6 years of SPY 1-minute bars from Alpaca, normalizes each session
to a percentage-change-from-open price path, clusters them using HDBSCAN
(density-based, finds natural families without pre-specifying the count),
and computes per-cluster per-minute statistics (% positive, % negative,
mean return, percentile bands).

HDBSCAN advantages over k-Shape for this use case:
- Does not require pre-specifying the number of clusters
- Identifies noise points (sessions that don't belong to any family)
- Handles clusters of varying density and shape
- Soft clustering membership probabilities quantify confidence

Writes a JSON fixture consumed by the thesis UI clustering study page:

    src/ui/src/lib/data/spy0dteClusters.js

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/price_clusters.py `
        --data-root .local/spy-clusters `
        --out src/ui/src/lib/data/spy0dteClusters.js `
        --years 6 --min-cluster-size 25
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import hdbscan
import numpy as np
from osi import closed_sessions

from lake.configuration import load_settings
from lake.providers.alpaca.client import AlpacaClient


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
    return np.array(paths, dtype=np.float64), valid_keys


def zscore_batch(paths: np.ndarray) -> np.ndarray:
    """Z-normalize every path (remove mean and scale)."""
    means = np.mean(paths, axis=1, keepdims=True)
    stds = np.std(paths, axis=1, keepdims=True)
    stds[stds < 1e-10] = 1.0
    return (paths - means) / stds


def hdbscan_cluster(
    paths: np.ndarray,
    min_cluster_size: int = 25,
    min_samples: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """HDBSCAN clustering on z-normalized paths.

    Returns (labels, probabilities). Labels: -1 = noise, 0..k-1 = cluster.
    """
    z_paths = zscore_batch(paths)
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        cluster_selection_method="eom",
    )
    labels = clusterer.fit_predict(z_paths)
    probabilities = clusterer.probabilities_

    unique = sorted(set(labels) - {-1})
    remap = {old: new for new, old in enumerate(unique)}
    remapped = np.array([remap.get(label, -1) for label in labels])
    return remapped, probabilities


def cluster_stats(
    paths: np.ndarray,
    labels: np.ndarray,
    keys: list[str],
    probabilities: np.ndarray | None = None,
) -> list[dict]:
    """Per-cluster statistics: centroid, percentile bands, % positive, count."""
    clusters: list[dict] = []
    unique = sorted(set(labels) - {-1})
    for cluster_id in unique:
        members = np.where(labels == cluster_id)[0]
        if len(members) == 0:
            continue
        member_paths = paths[members]
        centroid = np.mean(member_paths, axis=0)
        pct_positive = np.mean(member_paths > 0, axis=0) * 100
        pct_negative = np.mean(member_paths < 0, axis=0) * 100
        avg_return = float(np.mean(member_paths[:, -1]))
        p25 = np.percentile(member_paths, 25, axis=0)
        p75 = np.percentile(member_paths, 75, axis=0)
        p10 = np.percentile(member_paths, 10, axis=0)
        p90 = np.percentile(member_paths, 90, axis=0)
        entry: dict = {
            "cluster": cluster_id,
            "count": int(len(members)),
            "avgReturn": round(avg_return, 4),
            "sessions": [keys[i] for i in members],
            "centroid": [round(float(v), 4) for v in centroid],
            "pctPositive": [round(float(v), 2) for v in pct_positive],
            "pctNegative": [round(float(v), 2) for v in pct_negative],
            "p25": [round(float(v), 4) for v in p25],
            "p75": [round(float(v), 4) for v in p75],
            "p10": [round(float(v), 4) for v in p10],
            "p90": [round(float(v), 4) for v in p90],
        }
        if probabilities is not None:
            entry["avgConfidence"] = round(float(np.mean(probabilities[members])), 4)
        clusters.append(entry)
    clusters.sort(key=lambda c: -c["count"])
    return clusters


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--years", type=int, default=6)
    parser.add_argument("--min-cluster-size", type=int, default=25)
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

    labels, probabilities = hdbscan_cluster(
        paths, min_cluster_size=args.min_cluster_size
    )
    n_clusters = len(set(labels) - {-1})
    n_noise = int(np.sum(labels == -1))
    print(
        f"HDBSCAN complete: {n_clusters} clusters, "
        f"{n_noise} noise points ({n_noise / len(labels) * 100:.1f}%)",
        flush=True,
    )

    cluster_data = cluster_stats(paths, labels, keys, probabilities)
    print(f"computed stats for {len(cluster_data)} clusters", flush=True)

    pct_pos_overall = np.mean(paths > 0, axis=0) * 100
    pct_neg_overall = np.mean(paths < 0, axis=0) * 100

    fixture = {
        "meta": {
            "sessions": len(paths),
            "firstSession": keys[0] if keys else "",
            "lastSession": keys[-1] if keys else "",
            "clusters": n_clusters,
            "noisePoints": n_noise,
            "years": args.years,
            "minClusterSize": args.min_cluster_size,
            "normalized": "percentage change from open, padded to 390 minutes",
            "algorithm": "HDBSCAN (density-based, euclidean on z-normalized paths)",
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
        conf = cluster.get("avgConfidence")
        conf_str = f", confidence {conf:.2f}" if conf is not None else ""
        print(
            f"  cluster {cluster['cluster']}: {cluster['count']} sessions, "
            f"avg return {cluster['avgReturn']:+.2f}%{conf_str}",
            flush=True,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
