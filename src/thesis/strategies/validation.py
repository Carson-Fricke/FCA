"""Validation handoff for authored program bundles.

The harness JIT seam is the real validator: an authored Rust program either
compiles and runs, or the submit surface returns the compiler/engine failure.
This module keeps the injection seam so a composition can add checks (source
presence, export macro, size limits) before handing a bundle over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from thesis.strategies.bundles import StrategyBundle

ValidationPurpose = Literal["backtest", "study"]

#: What an authored program source must contain to be accepted pre-compile.
REQUIRED_SOURCE_MARKERS: tuple[str, ...] = (
    "harness_strategy_sdk",
    "export_strategy_program!",
)


class StrategyValidationError(RuntimeError):
    """Raised when a validator refuses a program bundle."""


@dataclass(frozen=True, slots=True)
class StrategyValidationRequest:
    """Immutable request sent to an injected validator."""

    bundle: StrategyBundle
    purpose: ValidationPurpose = "backtest"
    network_access: Literal["disabled"] = "disabled"
    program_boundary: str = "harness-strategy-sdk"

    @property
    def bundle_sha256(self) -> str:
        """Return the identity the validator must attest."""
        return self.bundle.bundle_sha256

    @property
    def source(self):
        """Return the authored Rust source files in the bundle."""
        return self.bundle.source

    @property
    def tests(self):
        """Return bundle tests for validation."""
        return self.bundle.tests


@dataclass(frozen=True, slots=True)
class StrategyValidationResult:
    """Validator decision and deterministic diagnostics."""

    accepted: bool
    diagnostics: tuple[str, ...] = ()
    validation_id: str | None = None

    def __post_init__(self) -> None:
        diagnostics = tuple(self.diagnostics)
        if not all(isinstance(item, str) for item in diagnostics):
            raise TypeError("validation diagnostics must be strings")
        object.__setattr__(self, "diagnostics", diagnostics)


class StrategySandboxValidator(Protocol):
    """Published injection boundary for a pre-compile validator."""

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        """Validate the authored program source before submission."""


def validation_request(
    bundle: StrategyBundle, *, purpose: ValidationPurpose = "backtest"
) -> StrategyValidationRequest:
    """Create the exact sandbox handoff request for a bundle."""
    return StrategyValidationRequest(bundle=bundle, purpose=purpose)


def validate_strategy_bundle(
    bundle: StrategyBundle,
    validator: StrategySandboxValidator,
    *,
    purpose: ValidationPurpose = "backtest",
) -> StrategyValidationResult:
    """Hand a bundle to the validator before any backtest launch is allowed."""
    request = validation_request(bundle, purpose=purpose)
    result = validator.validate(request)
    if not isinstance(result, StrategyValidationResult):
        raise TypeError("validator must return StrategyValidationResult")
    if not result.accepted:
        detail = "; ".join(result.diagnostics) or "sandbox validation was rejected"
        raise StrategyValidationError(detail)
    return result


def handoff_to_sandbox(
    bundle: StrategyBundle,
    validator: StrategySandboxValidator,
    *,
    purpose: ValidationPurpose = "backtest",
) -> StrategyValidationResult:
    """Explicitly named alias for the pre-launch validation handoff."""
    return validate_strategy_bundle(bundle, validator, purpose=purpose)


SandboxValidationRequest = StrategyValidationRequest
SandboxValidationResult = StrategyValidationResult
ValidationRequest = StrategyValidationRequest


__all__ = [
    "SandboxValidationRequest",
    "SandboxValidationResult",
    "StrategySandboxValidator",
    "StrategyValidationError",
    "StrategyValidationRequest",
    "StrategyValidationResult",
    "ValidationRequest",
    "handoff_to_sandbox",
    "validate_strategy_bundle",
    "validation_request",
]
