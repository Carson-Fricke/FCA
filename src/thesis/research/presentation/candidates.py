"""Extensible deterministic candidate generators; the decision model only ranks."""

import hashlib
import itertools
import json
from collections.abc import Callable
from typing import Any

from thesis.research.presentation.profiles import number

Generator = Callable[[dict[str, Any]], list[dict[str, Any]]]
GENERATORS: dict[str, Generator] = {}


def register_candidates(name: str, generate: Generator) -> None:
    """Register a local grammar generator; artifacts never supply executable code."""
    if not name or name in GENERATORS:
        raise ValueError("candidate generator is already registered or unnamed")
    GENERATORS[name] = generate


def panel(data: dict[str, Any], kind: str, encodings: dict[str, str]) -> dict[str, Any]:
    fields = {f["name"]: f for f in data["profiles"]}
    title = (
        kind.capitalize()
        + " · "
        + " × ".join(fields[n]["label"] for n in list(encodings.values())[:3])
    )
    identity = json.dumps(
        [data["instanceId"], data["name"], kind, encodings], sort_keys=True
    )
    return {
        "id": "suggested-" + hashlib.sha256(identity.encode()).hexdigest()[:14],
        "kind": kind,
        "title": title,
        "dataset": data["dataset"]["id"],
        "encodings": encodings,
        "description": f"{kind.capitalize()} of {title}. {data['row_meaning']}",
    }


def standard_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    fields = {f["name"]: f for f in data["profiles"]}
    analytical = [
        n
        for n, f in fields.items()
        if n != data["dataset"]["identity"] and f["cardinality"] > 1
    ]
    numeric = [n for n in analytical if fields[n]["field_type"] == "number"]
    measures = [
        n
        for n, f in fields.items()
        if f["field_type"] == "number"
        and f["cardinality"] > 0
        and n != data["dataset"]["identity"]
        and (not data["declared"] or n in data["measures"])
    ]
    dimensions = [
        n for n in analytical if not data["declared"] or n in data["dimensions"]
    ]
    ordered = [
        n
        for n in dimensions
        if fields[n]["field_type"] in {"number", "timestamp_ns"}
        and (not data["declared"] or n in data["ordered"])
    ]
    candidates = []

    # Sample no rows: eligibility is checked against the complete output.
    def unique(names: list[str]) -> bool:
        def coordinate(name: str, value: Any) -> Any:
            kind = fields[name]["field_type"]
            return (
                number(value)
                if kind == "number"
                else int(value)
                if kind == "timestamp_ns"
                else str(value)
            )

        keys = [
            tuple(coordinate(n, row[n]) for n in names)
            for row in data["rows"]
            if all(row.get(n) is not None for n in names)
        ]
        return len(keys) == len(set(keys))

    for x in ordered:
        for y in measures:
            if x == y:
                continue
            groups = [
                n
                for n in dimensions
                if n not in {x, y} and fields[n]["cardinality"] <= 12
            ]
            encodings = {"x": x, "y": y}
            if not unique([x]):
                group = next((g for g in groups if unique([x, g])), None)
                if not group:
                    continue
                encodings["series"] = group
            candidates.append(panel(data, "line", encodings))
    for x, y in itertools.combinations(dimensions, 2):
        if (
            fields[x]["cardinality"] > 80
            or fields[y]["cardinality"] > 80
            or not unique([x, y])
        ):
            continue
        if (
            data["total"] / max(1, fields[x]["cardinality"] * fields[y]["cardinality"])
            < 0.25
        ):
            continue
        for value in measures:
            if value not in {x, y}:
                candidates.append(
                    panel(data, "heatmap", {"x": x, "y": y, "value": value})
                )
    for x, y in itertools.combinations(numeric, 2):
        candidates.append(panel(data, "scatter", {"x": x, "y": y}))
        for color in measures:
            if color not in {x, y}:
                candidates.append(
                    panel(data, "scatter", {"x": x, "y": y, "color": color})
                )
    for x in dimensions:
        if fields[x]["cardinality"] > 40 or not unique([x]):
            continue
        for y in measures:
            if x != y:
                candidates.append(panel(data, "comparison", {"x": x, "y": y}))
    # Interleave grammars before the budget, so wide tables cannot crowd out heatmaps.
    grouped = {
        kind: [p for p in candidates if p["kind"] == kind]
        for kind in ("heatmap", "line", "scatter", "comparison")
    }
    return [
        p for group in itertools.zip_longest(*grouped.values()) for p in group if p
    ][:48]


register_candidates("standard", standard_candidates)


def candidates_for(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for generate in GENERATORS.values() for p in generate(data)][:64]
