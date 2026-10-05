"""Prompt/schema contracts prevent lossy repair and repeated authorization."""

from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    HYPOTHESIS_COMPLETION_SCHEMA,
)
from thesis.workflows.registry.hypothesis_development.prompts.model_creation import (
    _model_creation_revision_section,
    _model_creation_turn_prompt,
)
from thesis.workflows.registry.hypothesis_development.prompts.verdict import (
    _verdict_instructions,
)


def test_model_creation_argument_repair_preserves_operator_approved_research() -> None:
    prompt = _model_creation_turn_prompt([("user", "Test pairs mean reversion")])
    assert "repair only the encoding" in prompt
    assert "constant-output stub" in prompt
    assert "data-only falsifier suite" in prompt
    assert "negative control" in prompt
    assert "minimal diagnostic object" in _model_creation_revision_section(None, None)
    properties = HYPOTHESIS_COMPLETION_SCHEMA["properties"]
    assert isinstance(properties, dict)
    hypothesis = properties["hypothesis"]
    description = hypothesis["properties"]["falsifiers"]["description"]
    assert "complete operator-approved" in description
    assert "Data availability alone" in description


def test_verdict_explicit_loopback_is_authorization_not_another_question() -> None:
    prompt = _verdict_instructions()
    assert "authorizes target model_creation now" in prompt
    assert "target discovery when the candidate itself" in prompt
    assert "do not ask them to confirm the same direction again" in prompt
    assert "do not label the claim supported merely because data_ok is 1" in prompt
