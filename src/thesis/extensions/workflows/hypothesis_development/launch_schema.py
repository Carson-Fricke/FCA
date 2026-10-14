"""Hypothesis-development launch input: the operator's research request."""


def hypothesis_launch_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["idea"],
        "properties": {
            "idea": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8192,
                "description": "Preserve the operator's research request, including dates and constraints. Launch directly; the workflow owns research design and data-window setup.",
            },
            "investigation_id": {"type": "string", "minLength": 1},
        },
    }
