"""Validation handoff for authored program bundles.

The harness JIT seam reports compilation and execution failures. Neither JIT
compilation nor this module's structural checks establish a security sandbox.
Structural checks inspect text only and can accept malformed Rust or markers
inside comments. They do not enforce network, filesystem, process, or memory
isolation. The injection seam lets a composition supply additional checks.
"""

from __future__ import annotations

import re
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
    """Published validator seam; the historical name is not an isolation guarantee."""

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        """Validate the authored program source before submission."""


class StructuralStrategyValidator:
    """Check text structure only, not Rust syntax, runnable behavior, or security.

    Marker and regex checks are preflight diagnostics, not a Rust parser. No
    compiler, test runner, or security sandbox is invoked by this validator.
    """

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        """Return deterministic pre-JIT diagnostics without executing Rust."""
        bundle = request.bundle
        diagnostics: list[str] = []
        if tuple(file.path for file in bundle.source) != ("src/lib.rs",):
            diagnostics.append("current JIT requires a single src/lib.rs source")
        for file in bundle.source:
            try:
                source = file.content.decode("utf-8")
            except UnicodeError:
                diagnostics.append("Rust source must be UTF-8")
                continue
            if not all(marker in source for marker in REQUIRED_SOURCE_MARKERS):
                diagnostics.append(
                    "source must use harness_strategy_sdk and export_strategy_program!"
                )
            if (
                re.search(r"impl\s+(?:[\w:]+::)?StrategyProgram\s+for\s+", source)
                is None
            ):
                diagnostics.append("source must implement StrategyProgram")
            if re.search(
                r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]|#\s*\[\s*test\s*\]", source
            ):
                diagnostics.append("Rust tests belong under tests/, not src/")
            if re.search(r"\bmod\s+\w+\s*;|\binclude(?:_str|_bytes)?\s*!", source):
                diagnostics.append("current JIT source must be self-contained")
        for file in bundle.tests:
            try:
                text = file.content.decode("utf-8")
            except UnicodeError:
                diagnostics.append("Rust tests must be UTF-8")
                continue
            if not file.path.startswith("tests/") or not file.path.endswith(".rs"):
                diagnostics.append("Rust tests must live under tests/")
            if (
                re.search(r"#\s*\[\s*test\s*\]\s*(?:pub\s+)?fn\s+\w+\s*\(", text)
                is None
            ):
                diagnostics.append(
                    "each test file must contain a runnable #[test] function"
                )
        return StrategyValidationResult(
            accepted=not diagnostics,
            diagnostics=tuple(diagnostics),
            validation_id="structural-rust-v1",
        )


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
    "StructuralStrategyValidator",
    "StrategyValidationError",
    "StrategyValidationRequest",
    "StrategyValidationResult",
    "ValidationRequest",
    "handoff_to_sandbox",
    "validate_strategy_bundle",
    "validation_request",
]
