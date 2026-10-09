"""Bounded views of immutable harness exploratory output artifacts."""

import hashlib
import json
from collections.abc import Mapping


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
                        output[key]
                        for key in ("points", "events", "rows")
                        if key in output
                    ),
                    [],
                )
                entries.append(
                    {
                        "instanceId": instance,
                        "name": name,
                        "kind": output.get("kind"),
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
        raise KeyError(output_name)
    output = outputs[output_name]
    if not isinstance(output, Mapping):
        raise ValueError("exploratory output must be an object")
    row_key = next((key for key in ("points", "events", "rows") if key in output), None)
    if row_key is None:
        if len(json.dumps(dict(output), allow_nan=False)) > 32_000:
            raise ValueError(
                "output state exceeds inspection size; use the complete artifact export"
            )
        return {
            **identity,
            "instanceId": instance_id,
            "name": output_name,
            "output": dict(output),
            "nextOffset": None,
        }
    rows = output[row_key]
    if not isinstance(rows, list):
        raise ValueError("exploratory output records must be an array")
    page = dict(output)
    selected = []
    size = 0
    for row in rows[offset : offset + limit]:
        row_size = len(json.dumps(row, allow_nan=False))
        if size + row_size > 32_000:
            break
        selected.append(row)
        size += row_size
    if not selected and offset < len(rows):
        raise ValueError(
            "one output record exceeds inspection size; use the complete artifact export"
        )
    page[row_key] = selected
    next_offset = offset + len(selected)
    return {
        **identity,
        "instanceId": instance_id,
        "name": output_name,
        "output": page,
        "total": len(rows),
        "nextOffset": next_offset if next_offset < len(rows) else None,
    }
