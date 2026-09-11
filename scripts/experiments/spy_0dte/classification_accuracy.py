"""Classification accuracy study: how early can you identify the day's family?

For each minute N (1..390), classifies each session using only its price
path through minute N — nearest cluster mean under Euclidean distance on
z-normalized paths (the same metric HDBSCAN clusters with) — and measures
accuracy against the HDBSCAN cluster labels. Also computes per-cluster
per-minute percentile bands.

Outputs a JSON fixture consumed by the thesis UI clustering study page:

    src/ui/src/lib/data/spy0dteClassification.js

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/classification_accuracy.py `
        --data-root .local/spy-clusters `
        --out src/ui/src/lib/data/spy0dteClassification.js
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
from price_clusters import normalize_paths, zscore_batch


def classification_accuracy_by_minute(
    paths: np.ndarray,
    labels: np.ndarray,
    k: int,
    check_minutes: list[int],
) -> list[dict]:
    """For each check minute, classify by nearest z-normalized cluster mean."""
    n = len(paths)
    results: list[dict] = []

    for minute_n in check_minutes:
        if minute_n < 5 or minute_n > 390:
            continue
        truncated = paths[:, :minute_n]
        z_truncated = zscore_batch(truncated)

        centroids = np.zeros((k, minute_n))
        for j in range(k):
            members = np.where(labels == j)[0]
            if len(members) == 0:
                continue
            centroids[j] = np.mean(z_truncated[members], axis=0)

        # Vectorized nearest-centroid classification
        # z_truncated: (n, minute_n), centroids: (k, minute_n)
        # distances: (n, k)
        distances = np.sqrt(
            np.mean(
                (z_truncated[:, np.newaxis, :] - centroids[np.newaxis, :, :]) ** 2,
                axis=2,
            )
        )
        predictions = np.argmin(distances, axis=1)

        accuracy = float(np.mean(predictions == labels))
        results.append(
            {
                "minute": minute_n,
                "accuracy": round(accuracy, 4),
                "randomBaseline": round(1.0 / k, 4),
            }
        )

    return results


def cluster_percentile_bands(
    paths: np.ndarray, labels: np.ndarray, k: int
) -> list[dict]:
    """Per-cluster per-minute percentile bands around the centroid."""
    clusters: list[dict] = []
    unique = sorted(set(labels) - {-1})
    for cluster_id in unique:
        members = np.where(labels == cluster_id)[0]
        if len(members) == 0:
            continue
        member_paths = paths[members]
        centroid = np.mean(member_paths, axis=0)
        percentiles = {}
        for pct in (10, 25, 75, 90):
            values = np.percentile(member_paths, pct, axis=0)
            percentiles[f"p{pct}"] = [round(float(v), 4) for v in values]
        clusters.append(
            {
                "cluster": cluster_id,
                "count": int(len(members)),
                "centroid": [round(float(v), 4) for v in centroid],
                **percentiles,
            }
        )
    clusters.sort(key=lambda c: -c["count"])
    return clusters


def load_labels(keys: list[str], clusters_fixture: dict) -> tuple[np.ndarray, int]:
    """Reconstruct per-session labels from the clustering fixture."""
    key_to_index = {key: i for i, key in enumerate(keys)}
    labels = np.full(len(keys), -1, dtype=int)
    for cluster in clusters_fixture["clusters"]:
        for session_key in cluster["sessions"]:
            if session_key in key_to_index:
                labels[key_to_index[session_key]] = cluster["cluster"]
    return labels, int(clusters_fixture["meta"]["clusters"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--step-minutes", type=int, default=10)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.out = args.out.resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    cache_dir = args.data_root / "bar-cache"
    if not cache_dir.is_dir():
        print("bar cache not found — run price_clusters.py first", flush=True)
        return 1

    raw_bars: dict[str, list[float]] = {}
    for cache_file in sorted(cache_dir.glob("*.json")):
        raw_bars[cache_file.stem] = json.loads(cache_file.read_text(encoding="utf-8"))

    paths, keys = normalize_paths(raw_bars, target_length=390)
    print(f"loaded {len(paths)} price paths", flush=True)

    clusters_file = args.out.parent / "spy0dteClusters.js"
    if not clusters_file.is_file():
        print("cluster fixture not found — run price_clusters.py first", flush=True)
        return 1
    clusters_text = clusters_file.read_text(encoding="utf-8")
    match = re.search(r"export const clusters = (\{.*\});", clusters_text, re.S)
    if not match:
        print("cannot parse cluster fixture", flush=True)
        return 1
    cluster_fixture = json.loads(match.group(1))
    k = int(cluster_fixture["meta"]["clusters"])

    labels, k = load_labels(keys, cluster_fixture)
    labeled = np.where(labels >= 0)[0]
    paths_labeled = paths[labeled]
    labels_labeled = labels[labeled]
    print(
        f"labeled {len(labeled)}/{len(paths)} paths across {k} clusters",
        flush=True,
    )

    check_minutes = list(range(args.step_minutes, 391, args.step_minutes))
    accuracy = classification_accuracy_by_minute(
        paths_labeled, labels_labeled, k, check_minutes
    )
    print(
        f"classification accuracy computed at {len(accuracy)} check minutes",
        flush=True,
    )

    iqr_bands = cluster_percentile_bands(paths_labeled, labels_labeled, k)
    print(f"percentile bands computed for {len(iqr_bands)} clusters", flush=True)

    fixture = {
        "meta": {
            "sessions": len(labeled),
            "clusters": k,
            "checkMinutes": check_minutes,
            "randomBaseline": round(1.0 / k, 4),
            "metric": "euclidean on z-normalized paths",
        },
        "accuracyByMinute": accuracy,
        "iqrBands": iqr_bands,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "/* Generated by scripts/experiments/spy_0dte/classification_accuracy.py"
        " - do not edit. */\n"
        "export const classification = " + json.dumps(fixture, indent=1) + ";\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}", flush=True)

    baseline = 1.0 / k
    print(
        f"\nClassification accuracy by minute (random baseline = {baseline:.1%}):",
        flush=True,
    )
    for entry in accuracy:
        headroom = 1.0 - baseline
        filled = entry["accuracy"] - baseline
        bar = "#" * max(0, int(filled / headroom * 40)) if headroom > 0 else ""
        print(
            f"  minute {entry['minute']:>3}: {entry['accuracy']:>6.1%} {bar}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
