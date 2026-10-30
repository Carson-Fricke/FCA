"""Bounded views of immutable harness exploratory output artifacts."""

import hashlib
import json
from collections.abc import Mapping

from thesis_runtime.contracts.failures import ToolFailureError


def _records_output(output: Mapping) -> Mapping:
    """Host-sealed observations page their canonical nested table unchanged."""
    if output.get("kind") == "causal_observations":
        dataset = output.get("dataset")
        if not isinstance(dataset, Mapping):
            raise ValueError("causal observations require a dataset")
        return dataset
    return output


def _browser_value(value: object) -> object:
    """Keep exact integral evidence when JSON crosses JavaScript's safe range."""
    if type(value) is int and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: _browser_value(cell) for key, cell in value.items()}
    if isinstance(value, list):
        return [_browser_value(cell) for cell in value]
    return value


def output_page(
    job_id: str,
    content: str,
    *,
    instance_id: str | None = None,
    output_name: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, object]:
    """Inspect named sequences/events/tables without replacing artifact authority."""
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 500
    ):
        raise ValueError("offset must be nonnegative; limit must be from 1 to 500")
    document = json.loads(content)
    if (
        not isinstance(document, dict)
        or document.get("job_id") != job_id
        or document.get("schema_version") != 1
    ):
        raise ValueError("exploratory artifact has mismatched identity or schema")
    programs = document.get("programs")
    if not isinstance(programs, dict):
        raise ValueError("exploratory artifact has no program output map")
    identity = {
        "job_id": job_id,
        "artifactName": "exploratory_outputs.json",
        "artifactSha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    if output_name is None:
        entries = []
        for instance, outputs in programs.items():
            if not isinstance(outputs, dict):
                raise ValueError("exploratory program outputs must be objects")
            for name, output in outputs.items():
                if not isinstance(output, dict):
                    raise ValueError("exploratory output must be an object")
                values = next(
                    (
                        _records_output(output)[key]
                        for key in ("points", "events", "rows")
                        if key in _records_output(output)
                    ),
                    None,
                )
                entries.append(
                    {
                        "instanceId": instance,
                        "name": name,
                        "kind": output.get("kind"),
                        "authority": "harness_replay"
                        if output.get("kind") == "causal_observations"
                        else "authored",
                        "count": len(values) if isinstance(values, list) else None,
                    }
                )
        return {
            **identity,
            "outputs": entries[offset : offset + limit],
            "total": len(entries),
            "nextOffset": offset + limit if offset + limit < len(entries) else None,
        }
    if instance_id is None:
        raise ValueError("instance_id is required when selecting an output")
    outputs = programs.get(instance_id)
    if not isinstance(outputs, dict) or output_name not in outputs:
        choices = [
            {"instance_id": instance, "output_name": name}
            for instance, entries in programs.items()
            if isinstance(entries, dict)
            for name in entries
        ]
        raise ToolFailureError(
            {
                "kind": "missing_selection",
                "statusCode": 404,
                "message": f"Output {output_name!r} does not exist in instance {instance_id!r}.",
                "certainty": "not_started",
                "retry_safe": True,
                "availableSelections": choices,
                "nextArguments": {"job_id": job_id},
                "retryGuidance": "Select an available instance_id/output_name pair, or list the job outputs with nextArguments.",
            }
        )
    output = outputs[output_name]
    if not isinstance(output, Mapping):
        raise ValueError("exploratory output must be an object")
    records = _records_output(output)
    row_key = next(
        (key for key in ("points", "events", "rows") if key in records), None
    )
    if row_key is None:
        if len(json.dumps(dict(output), allow_nan=False)) > 32_000:
            raise ValueError(
                "output state exceeds inspection size; use the complete artifact export"
            )
        return {
            **identity,
            "instanceId": instance_id,
            "name": output_name,
            "output": _browser_value(dict(output)),
            "authority": "authored",
            "total": 0,
            "nextOffset": None,
        }
    rows = records[row_key]
    if not isinstance(rows, list):
        raise ValueError("exploratory output records must be an array")
    page = dict(output)
    selected = []
    size = 0
    for row in rows[offset : offset + limit]:
        row_size = len(json.dumps(row, allow_nan=False))
        if size + row_size > 32_000:
            break
        selected.append(_browser_value(row))
        size += row_size
    if not selected and offset < len(rows):
        raise ValueError(
            "one output record exceeds inspection size; use the complete artifact export"
        )
    if output.get("kind") == "causal_observations":
        page["dataset"] = {**records, row_key: selected}
    else:
        page[row_key] = selected
    next_offset = offset + len(selected)
    return {
        **identity,
        "instanceId": instance_id,
        "name": output_name,
        "output": page,
        "authority": "harness_replay"
        if output.get("kind") == "causal_observations"
        else "authored",
        "total": len(rows),
        "nextOffset": next_offset if next_offset < len(rows) else None,
    }
