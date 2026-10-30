"""Full-output field profiles; observed shape never invents scientific meaning."""

import math
from collections import Counter
from typing import Any

from thesis.workspaces.exploration import output_page


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def profile_field(field: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [row.get(field["name"]) for row in rows]
    present = [v for v in values if v is not None]
    counts = Counter(
        str(number(v))
        if field["field_type"] == "number" and number(v) is not None
        else str(v)
        for v in present
    )
    result = {
        **field,
        "count": len(values),
        "missing": len(values) - len(present),
        "cardinality": len(counts),
        "top_values": [
            {"value": v[:160], "count": n} for v, n in counts.most_common(8)
        ],
        "range": None,
        "quantiles": None,
        "histogram": [],
    }
    if field["field_type"] == "timestamp_ns" and present:
        times = sorted(int(v) for v in present)
        result["range"] = [str(times[0]), str(times[-1])]
        result["quantiles"] = [
            str(times[round((len(times) - 1) * q)]) for q in (0, 0.25, 0.5, 0.75, 1)
        ]
        return result
    numbers = sorted(n for v in present if (n := number(v)) is not None)
    if numbers and len(numbers) == len(present):
        low, high = numbers[0], numbers[-1]
        result["range"] = [low, high]
        result["quantiles"] = [
            numbers[round((len(numbers) - 1) * q)] for q in (0, 0.25, 0.5, 0.75, 1)
        ]
        bins = min(12, len(counts))
        width = (high - low) / bins if high != low else 1
        buckets = [0] * bins
        for value in numbers:
            buckets[min(bins - 1, int((value - low) / width))] += 1
        result["histogram"] = [
            {
                "lower": low + i * width,
                "upper": high if i == bins - 1 else low + (i + 1) * width,
                "count": n,
            }
            for i, n in enumerate(buckets)
        ]
    return result


def datasets(job_id: str, content: str) -> list[dict[str, Any]]:
    """Decode the current artifact and profile every row, never a browser page."""
    import json

    output_page(job_id, content)  # Canonical artifact identity/schema validation.
    programs = json.loads(content)["programs"]
    result = []
    for instance, outputs in programs.items():
        descriptions = {}
        for output in outputs.values():
            if output.get("kind") == "dataset_semantics":
                semantics = output["semantics"]
                name = semantics["dataset"]["output"]
                if name in descriptions:
                    raise ValueError("conflicting dataset semantics")
                descriptions[name] = semantics
        for name, output in outputs.items():
            kind = output.get("kind")
            if kind in {"table", "causal_observations"}:
                table = output["dataset"] if kind == "causal_observations" else output
                columns = table["columns"]
                rows = [dict(zip(columns, row, strict=True)) for row in table["rows"]]
            elif kind in {"series", "events"}:
                rows = [
                    {
                        **row.get("fields", {}),
                        **{k: v for k, v in row.items() if k != "fields"},
                    }
                    for row in output["points" if kind == "series" else "events"]
                ]
                columns = list(dict.fromkeys(k for row in rows for k in row))
            else:
                continue
            if not columns:
                continue
            semantics = descriptions.get(name)
            if semantics:
                binding = semantics["dataset"]
                if semantics.get("schema_version") != 1 or any(
                    not f.get("description", "").strip() for f in binding["fields"]
                ):
                    raise ValueError(
                        "outdated dataset semantics: fields require descriptions"
                    )
                fields = binding["fields"]
            else:
                fields = []
                for column in columns:
                    present = [r[column] for r in rows if r.get(column) is not None]
                    typed_time = (
                        kind in {"series", "events"}
                        and column
                        in {
                            "event_time_ns",
                            "available_at_ns",
                        }
                    ) or (
                        kind == "causal_observations"
                        and column
                        in {
                            "decision_at_ns",
                            "outcome_due_at_ns",
                            "outcome_available_at_ns",
                        }
                    )
                    field_type = (
                        "timestamp_ns"
                        if typed_time
                        else "boolean"
                        if present and all(isinstance(v, bool) for v in present)
                        else "number"
                        if present and all(number(v) is not None for v in present)
                        else "string"
                    )
                    fields.append(
                        {
                            "name": column,
                            "label": column,
                            "description": "Published event time."
                            if typed_time
                            else "Scientific meaning was not published.",
                            "field_type": field_type,
                            "unit": "ns" if typed_time else None,
                        }
                    )
                binding = {
                    "id": name,
                    "output": name,
                    "fields": fields,
                    "identity": None,
                    "selection_group": None,
                    "event_time": "event_time_ns"
                    if kind in {"series", "events"}
                    else None,
                    "available_at": "available_at_ns"
                    if kind in {"series", "events"}
                    else None,
                }
            if any(f["name"] not in columns for f in fields):
                raise ValueError("dataset field is absent from its evidence")
            profiles = [profile_field(field, rows) for field in fields]
            item = {
                "instanceId": instance,
                "name": name,
                "dataset": binding,
                "row_meaning": semantics["row_meaning"]
                if semantics
                else "Meaning was not published; recommendations use observed data shape only.",
                "declared": bool(semantics),
                "grain": semantics["grain"] if semantics else None,
                "dimensions": semantics["dimensions"] if semantics else [],
                "measures": semantics["measures"] if semantics else [],
                "ordered": semantics["ordered"] if semantics else [],
                "profiles": profiles,
                "rows": rows,
                "total": len(rows),
            }
            result.append(item)
    return result
