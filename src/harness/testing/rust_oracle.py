"""Rust-core determinism and parity oracle.

This module is the Rust-backed sibling of the Zig parity oracle
(:mod:`harness.runtime.zig_backtest`).  It drives the staged Rust core through
the PyO3 extension module :mod:`harness._rust` — compiled from
``src/harness/rust/crates/harness-py`` by the setuptools-rust build backend — and compares
the canonical determinism artifacts the core produces against committed golden
artifacts byte-for-byte.  A mismatch raises :class:`RustOracleError` carrying an
:class:`ArtifactDiffReport` that names every differing artifact, the first
differing byte offset, both payload digests and lengths, and a hex window around
the divergence.

The oracle mirrors the determinism-harness contract in
:mod:`harness.testing.determinism`: a :class:`RustOracleProgram` pins a
deterministic operation vector, the Python reference produces the golden
artifacts for that program, and the Rust core must reproduce them exactly.
Run it from pytest with::

    uv run --extra dev pytest -p no:cacheprovider tests/determinism/test_rust_oracle.py

Golden artifacts live under ``src/harness/testing/golden/rust_oracle/<label>/``.
Regenerate them after an intentional program change with::

    uv run --extra dev python -c "from harness.testing.rust_oracle import DEFAULT_PROGRAM, write_golden; write_golden(DEFAULT_PROGRAM)"

The oracle is additive: it does not replace the ctypes ``harness._native``
loader or the Zig-driven runner, and it never reads wall time, unseeded
randomness, or the network.  The Rust core is a staged port, so the operation
registry is deliberately small and extensible; new Rust surfaces register a
:class:`_OperationSpec` with an independent Python reference and a Rust driver,
and the comparison machinery is unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Final

from harness.configuration.hashing import configuration_hash
from harness.testing.determinism import (
    ArtifactCanonicalizer,
    DeterminismArtifact,
    DeterminismManifest,
)

#: Canonical schema tag for one Rust oracle program.
RUST_ORACLE_SCHEMA: Final = "fca-rust-oracle-program-v1"

#: Canonical schema tag for one Rust oracle result artifact.
RUST_ORACLE_RESULTS_SCHEMA: Final = "fca-rust-oracle-results-v1"

#: Root of the committed golden artifacts.
RUST_GOLDEN_ROOT: Final = Path(__file__).resolve().parent / "golden" / "rust_oracle"

#: Default deterministic vector length and seed for the resident program.
DEFAULT_RUST_ORACLE_COUNT: Final = 256
DEFAULT_RUST_ORACLE_SEED: Final = 20_260_910

_BUILD_HINT: Final = (
    "The Rust core extension `harness._rust` is built from "
    "`src/harness/rust/crates/harness-py` by the setuptools-rust build backend. Run "
    "`uv sync --locked --extra dev` (or `uv run --extra dev ...`) from the "
    "repository root to compile it, then retry."
)

_MASK64: Final = (1 << 64) - 1
_MULTIPLIER: Final = 6_364_136_223_846_793_005
_INCREMENT: Final = 1_442_695_040_888_963_407

_BOUNDARY_VALUES: Final = (
    0,
    1,
    -1,
    2,
    -2,
    2**31 - 1,
    -(2**31),
    2**32,
    2**62,
    -(2**62),
    2**63 - 1,
    -(2**63),
)

_DIFF_CONTEXT: Final = 12


class RustOracleUnavailable(RuntimeError):
    """The staged Rust core extension is absent, unloadable, or incomplete."""


class RustOracleConfigurationError(ValueError):
    """A Rust oracle program or golden layout is misconfigured."""


class RustOracleError(AssertionError):
    """Rust-produced artifacts diverged from the golden artifacts.

    The report is attached as :attr:`report` so callers can inspect every
    artifact-level difference rather than parsing the message.
    """

    def __init__(self, report: ArtifactDiffReport) -> None:
        self.report = report
        super().__init__("rust oracle parity failed:\n" + report.summary())


def deterministic_vector(count: int, seed: int) -> tuple[int, ...]:
    """Return a deterministic signed-``int64`` vector of length ``count``.

    The first entries are pinned boundaries (zero, both signs, and the
    ``int64`` extremes); the remainder come from a pinned 64-bit linear
    congruential generator so the golden artifact is a real fingerprint of the
    core's behavior rather than a hand-written table.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise RustOracleConfigurationError("count must be a non-negative integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise RustOracleConfigurationError("seed must be an integer")
    values: list[int] = []
    for value in _BOUNDARY_VALUES:
        if len(values) >= count:
            break
        values.append(value)
    state = seed & _MASK64
    while len(values) < count:
        state = (state * _MULTIPLIER + _INCREMENT) & _MASK64
        values.append(state - (1 << 64) if state >= (1 << 63) else state)
    return tuple(values)


@dataclass(frozen=True, slots=True)
class RustOracleOperation:
    """One registry operation applied to one deterministic input value."""

    op: str
    value: int

    def __post_init__(self) -> None:
        if self.op not in _OPERATIONS:
            raise RustOracleConfigurationError(
                f"unknown Rust oracle operation {self.op!r}; known operations: "
                + ", ".join(sorted(_OPERATIONS))
            )
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise RustOracleConfigurationError("operation value must be an integer")


@dataclass(frozen=True, slots=True)
class RustOracleProgram:
    """Complete deterministic input for one Rust oracle run.

    ``label`` names the committed golden directory; ``operations`` is the
    ordered operation vector both the Python reference and the Rust core
    execute.
    """

    label: str
    operations: tuple[RustOracleOperation, ...] | Sequence[RustOracleOperation]
    schema: str = RUST_ORACLE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise RustOracleConfigurationError("label must be a non-empty string")
        if self.label != self.label.strip() or any(
            part in {"", ".", ".."} for part in self.label.split("/")
        ):
            raise RustOracleConfigurationError(
                "label must be a relative slash-separated path with no traversal"
            )
        operations = tuple(self.operations)
        if not operations:
            raise RustOracleConfigurationError("program must declare at least one op")
        if not all(isinstance(item, RustOracleOperation) for item in operations):
            raise RustOracleConfigurationError(
                "operations must contain RustOracleOperation values"
            )
        object.__setattr__(self, "operations", operations)

    @property
    def configuration(self) -> Mapping[str, object]:
        """Return the portable inputs that identify this program."""
        return {
            "schema": self.schema,
            "label": self.label,
            "operations": [
                {"op": operation.op, "value": operation.value}
                for operation in self.operations
            ],
        }

    @property
    def configuration_hash(self) -> str:
        """Return the canonical hash of every pinned program input."""
        return configuration_hash(self.configuration)

    def reference_results(self) -> tuple[int, ...]:
        """Return the Python reference result for every operation."""
        return tuple(
            _OPERATIONS[operation.op].reference(operation.value)
            for operation in self.operations
        )

    def drive_results(self, core: ModuleType) -> tuple[int, ...]:
        """Drive the Rust core and return one result per operation."""
        return tuple(
            _OPERATIONS[operation.op].driver(core, operation.value)
            for operation in self.operations
        )


@dataclass(frozen=True, slots=True)
class RustOracleRun:
    """Inspectable output of one Rust oracle program execution."""

    program: RustOracleProgram
    manifest: DeterminismManifest
    artifacts: tuple[DeterminismArtifact, ...]

    @property
    def manifest_hash(self) -> str:
        """Return the canonical digest of the run manifest."""
        return self.manifest.sha256

    @property
    def payloads(self) -> dict[str, bytes]:
        """Return artifact bytes plus the canonical manifest bytes."""
        payloads = {artifact.name: artifact.payload for artifact in self.artifacts}
        payloads["core/manifest.json"] = self.manifest.canonical_bytes
        return payloads


@dataclass(frozen=True, slots=True)
class ArtifactDiff:
    """One artifact-level difference between golden and actual output."""

    name: str
    kind: str
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    expected_length: int | None = None
    actual_length: int | None = None
    offset: int | None = None
    expected_excerpt: str | None = None
    actual_excerpt: str | None = None
    detail: str | None = None

    def describe(self) -> str:
        """Return an actionable, multi-line description of this difference."""
        if self.detail is not None:
            return f"{self.name}: {self.detail}"
        if self.kind == "missing":
            return (
                f"{self.name}: golden artifact is missing from the Rust output "
                "(the core produced no bytes for this artifact)"
            )
        if self.kind == "unexpected":
            return (
                f"{self.name}: Rust output contains an artifact that golden "
                "artifacts do not declare"
            )
        offset = self.offset if self.offset is not None else 0
        lines = [
            f"{self.name}: byte mismatch at offset {offset} "
            f"(golden {self.expected_length} bytes sha256 "
            f"{_short_digest(self.expected_sha256)}, actual {self.actual_length} "
            f"bytes sha256 {_short_digest(self.actual_sha256)})"
        ]
        if self.expected_excerpt is not None:
            lines.append(f"  golden[{offset}:] = {self.expected_excerpt}")
        if self.actual_excerpt is not None:
            lines.append(f"  actual[{offset}:] = {self.actual_excerpt}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ArtifactDiffReport:
    """Every artifact-level difference between golden and actual output."""

    diffs: tuple[ArtifactDiff, ...]

    @property
    def equal(self) -> bool:
        """Return whether every artifact matched byte-for-byte."""
        return not self.diffs

    @property
    def artifact_names(self) -> tuple[str, ...]:
        """Return the names of every differing artifact in canonical order."""
        return tuple(diff.name for diff in self.diffs)

    def summary(self) -> str:
        """Return an actionable summary naming every difference."""
        if self.equal:
            return "rust oracle artifacts are byte-identical to golden artifacts"
        return "\n".join(diff.describe() for diff in self.diffs)


# --------------------------------------------------------------------------
# Operation registry
# --------------------------------------------------------------------------


def _smoke_roundtrip_reference(value: int) -> int:
    return value


def _smoke_roundtrip_driver(core: ModuleType, value: int) -> int:
    # `core` is the dynamically imported PyO3 extension; a constant getattr is
    # the only typed access available on a `ModuleType`.
    function = getattr(core, "smoke_roundtrip")  # noqa: B009
    result = function(value)
    if isinstance(result, bool) or not isinstance(result, int):
        raise RustOracleError(
            ArtifactDiffReport(
                (
                    ArtifactDiff(
                        name="core/results.json",
                        kind="type",
                        detail=(
                            "Rust smoke_roundtrip returned "
                            f"{type(result).__name__}, expected int"
                        ),
                    ),
                )
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class _OperationSpec:
    reference: Callable[[int], int]
    driver: Callable[[ModuleType, int], int]


_OPERATIONS: Final[Mapping[str, _OperationSpec]] = MappingProxyType(
    {
        "smoke_roundtrip": _OperationSpec(
            _smoke_roundtrip_reference, _smoke_roundtrip_driver
        )
    }
)


# --------------------------------------------------------------------------
# Rust core loading
# --------------------------------------------------------------------------


def load_rust_core() -> ModuleType:
    """Import and validate the staged Rust core extension.

    Raises :class:`RustOracleUnavailable` when the extension is missing, cannot
    be imported, or does not expose the registered operations.
    """
    try:
        module = import_module("harness._rust")
    except Exception as error:  # pragma: no cover - exercised via monkeypatch
        raise RustOracleUnavailable(
            f"the Rust core extension could not be imported: {error}. {_BUILD_HINT}"
        ) from error
    missing = sorted(
        operation
        for operation in _OPERATIONS
        if not callable(getattr(module, operation, None))
    )
    if missing:
        raise RustOracleUnavailable(
            "the Rust core extension does not expose the registered operations: "
            + ", ".join(missing)
            + f". {_BUILD_HINT}"
        )
    return module


# --------------------------------------------------------------------------
# Program execution and golden artifacts
# --------------------------------------------------------------------------


def _result_artifact(
    program: RustOracleProgram, results: Sequence[int]
) -> dict[str, object]:
    if len(results) != len(program.operations):
        raise RustOracleConfigurationError(
            "result count does not match the program operation count"
        )
    return {
        "schema": RUST_ORACLE_RESULTS_SCHEMA,
        "label": program.label,
        "configuration_hash": program.configuration_hash,
        "operations": [
            {
                "index": index,
                "op": operation.op,
                "value": operation.value,
                "result": result,
            }
            for index, (operation, result) in enumerate(
                zip(program.operations, results, strict=True)
            )
        ],
    }


def _build_run(program: RustOracleProgram, results: Sequence[int]) -> RustOracleRun:
    artifacts = ArtifactCanonicalizer.artifacts(
        {"core/results.json": _result_artifact(program, results)}
    )
    manifest = ArtifactCanonicalizer.manifest(program.configuration, artifacts)
    return RustOracleRun(program=program, manifest=manifest, artifacts=artifacts)


def reference_run(program: RustOracleProgram) -> RustOracleRun:
    """Build the run from the independent Python reference results."""
    if not isinstance(program, RustOracleProgram):
        raise RustOracleConfigurationError("program must be a RustOracleProgram")
    return _build_run(program, program.reference_results())


def run_rust_program(
    program: RustOracleProgram, *, core: ModuleType | None = None
) -> RustOracleRun:
    """Drive the Rust core over ``program`` and return its canonical run."""
    if not isinstance(program, RustOracleProgram):
        raise RustOracleConfigurationError("program must be a RustOracleProgram")
    rust_core = load_rust_core() if core is None else core
    return _build_run(program, program.drive_results(rust_core))


def golden_directory(program: RustOracleProgram, root: Path | None = None) -> Path:
    """Return the golden directory that holds ``program``'s artifacts."""
    return (RUST_GOLDEN_ROOT if root is None else root) / program.label


def golden_payloads(
    program: RustOracleProgram, root: Path | None = None
) -> dict[str, bytes]:
    """Read the committed golden payloads for ``program``.

    Keys are artifact names relative to the golden directory; the canonical
    manifest bytes are stored as ``core/manifest.json`` alongside the artifact.
    """
    directory = golden_directory(program, root)
    if not directory.is_dir():
        raise RustOracleConfigurationError(
            f"golden directory is missing: {directory}; regenerate it with "
            "harness.testing.rust_oracle.write_golden"
        )
    payloads: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            payloads[path.relative_to(directory).as_posix()] = path.read_bytes()
    if not payloads:
        raise RustOracleConfigurationError(f"golden directory is empty: {directory}")
    return payloads


def write_golden(program: RustOracleProgram, root: Path | None = None) -> Path:
    """Write the Python-reference payloads as the committed golden artifacts."""
    directory = golden_directory(program, root)
    for name, payload in reference_run(program).payloads.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return directory


def diff_payloads(
    expected: Mapping[str, bytes], actual: Mapping[str, bytes]
) -> ArtifactDiffReport:
    """Compare two named payload maps and report every actionable difference."""
    diffs: list[ArtifactDiff] = []
    for name in sorted(set(expected) | set(actual)):
        golden = expected.get(name)
        produced = actual.get(name)
        if golden is None:
            assert produced is not None
            diffs.append(
                ArtifactDiff(
                    name=name,
                    kind="unexpected",
                    actual_sha256=_sha256(produced),
                    actual_length=len(produced),
                )
            )
        elif produced is None:
            diffs.append(
                ArtifactDiff(
                    name=name,
                    kind="missing",
                    expected_sha256=_sha256(golden),
                    expected_length=len(golden),
                )
            )
        elif golden != produced:
            offset = _first_difference_offset(golden, produced)
            diffs.append(
                ArtifactDiff(
                    name=name,
                    kind="bytes",
                    expected_sha256=_sha256(golden),
                    actual_sha256=_sha256(produced),
                    expected_length=len(golden),
                    actual_length=len(produced),
                    offset=offset,
                    expected_excerpt=_hex_context(golden, offset),
                    actual_excerpt=_hex_context(produced, offset),
                )
            )
    return ArtifactDiffReport(tuple(diffs))


def assert_golden_is_current(
    program: RustOracleProgram, root: Path | None = None
) -> ArtifactDiffReport:
    """Assert the committed goldens equal the Python-reference payloads."""
    report = diff_payloads(
        golden_payloads(program, root), reference_run(program).payloads
    )
    if not report.equal:
        raise RustOracleError(report)
    return report


def assert_rust_matches_golden(
    program: RustOracleProgram,
    *,
    core: ModuleType | None = None,
    root: Path | None = None,
) -> RustOracleRun:
    """Drive the Rust core and assert byte-identity with the golden artifacts."""
    run = run_rust_program(program, core=core)
    report = diff_payloads(golden_payloads(program, root), run.payloads)
    if not report.equal:
        raise RustOracleError(report)
    return run


# --------------------------------------------------------------------------
# Byte-level diff support
# --------------------------------------------------------------------------


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _short_digest(digest: str | None) -> str:
    return "none" if digest is None else digest[:12]


def _first_difference_offset(left: bytes, right: bytes) -> int:
    for index, (left_byte, right_byte) in enumerate(zip(left, right, strict=False)):
        if left_byte != right_byte:
            return index
    return min(len(left), len(right))


def _hex_context(payload: bytes, offset: int) -> str:
    if offset >= len(payload):
        return ""
    window = payload[offset : offset + _DIFF_CONTEXT]
    rendered = " ".join(f"{byte:02x}" for byte in window)
    return rendered if len(payload) - offset <= _DIFF_CONTEXT else rendered + " ..."


# --------------------------------------------------------------------------
# Resident program
# --------------------------------------------------------------------------


def default_program() -> RustOracleProgram:
    """Return the resident Rust oracle program over the default vector."""
    values = deterministic_vector(DEFAULT_RUST_ORACLE_COUNT, DEFAULT_RUST_ORACLE_SEED)
    return RustOracleProgram(
        label="smoke-roundtrip",
        operations=tuple(
            RustOracleOperation(op="smoke_roundtrip", value=value) for value in values
        ),
    )


DEFAULT_PROGRAM: Final[RustOracleProgram] = default_program()


__all__ = [
    "DEFAULT_PROGRAM",
    "DEFAULT_RUST_ORACLE_COUNT",
    "DEFAULT_RUST_ORACLE_SEED",
    "RUST_GOLDEN_ROOT",
    "RUST_ORACLE_RESULTS_SCHEMA",
    "RUST_ORACLE_SCHEMA",
    "ArtifactDiff",
    "ArtifactDiffReport",
    "RustOracleConfigurationError",
    "RustOracleError",
    "RustOracleOperation",
    "RustOracleProgram",
    "RustOracleRun",
    "RustOracleUnavailable",
    "assert_golden_is_current",
    "assert_rust_matches_golden",
    "default_program",
    "deterministic_vector",
    "diff_payloads",
    "golden_directory",
    "golden_payloads",
    "load_rust_core",
    "reference_run",
    "run_rust_program",
    "write_golden",
]
