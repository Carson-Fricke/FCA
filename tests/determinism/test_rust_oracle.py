"""Rust-core determinism parity oracle tests.

These tests drive the staged Rust core through the :mod:`harness._rust` PyO3
extension and compare its canonical determinism artifacts byte-for-byte against
the committed golden artifacts under
``src/harness/testing/golden/rust_oracle``.  When the extension has not been
built the Rust tests skip with the oracle's explicit build instructions so the
default ``pytest`` run stays green; the pure diff-machinery tests always run.
"""

from __future__ import annotations

from types import ModuleType

import pytest

from harness.testing import rust_oracle
from harness.testing.rust_oracle import (
    DEFAULT_PROGRAM,
    ArtifactDiffReport,
    RustOracleConfigurationError,
    RustOracleError,
    RustOracleOperation,
    RustOracleProgram,
    RustOracleUnavailable,
    assert_golden_is_current,
    assert_rust_matches_golden,
    deterministic_vector,
    diff_payloads,
    golden_payloads,
    load_rust_core,
    reference_run,
    run_rust_program,
)


@pytest.fixture(scope="module")
def rust_core() -> ModuleType:
    """Load the built Rust core extension, or skip with build instructions."""
    try:
        return load_rust_core()
    except RustOracleUnavailable as error:
        pytest.skip(f"Rust core extension is not built: {error}")


# --------------------------------------------------------------------------
# Cross-language parity: Rust core versus committed golden artifacts
# --------------------------------------------------------------------------


def test_rust_core_reproduces_the_committed_golden_artifacts(
    rust_core: ModuleType,
) -> None:
    run = assert_rust_matches_golden(DEFAULT_PROGRAM, core=rust_core)
    assert (
        run.program.configuration_hash
        == reference_run(DEFAULT_PROGRAM).manifest.configuration_hash
    )
    assert (
        run.manifest.artifact_hashes
        == reference_run(DEFAULT_PROGRAM).manifest.artifact_hashes
    )


def test_committed_golden_artifacts_match_the_python_reference() -> None:
    assert_golden_is_current(DEFAULT_PROGRAM)


def test_rust_oracle_is_deterministic_across_runs(rust_core: ModuleType) -> None:
    first = run_rust_program(DEFAULT_PROGRAM, core=rust_core)
    second = run_rust_program(DEFAULT_PROGRAM, core=rust_core)
    assert first.manifest_hash == second.manifest_hash
    assert first.payloads == second.payloads


def test_rust_oracle_matches_reference_artifacts(rust_core: ModuleType) -> None:
    rust_run = run_rust_program(DEFAULT_PROGRAM, core=rust_core)
    reference = reference_run(DEFAULT_PROGRAM)
    assert rust_run.payloads == reference.payloads
    assert rust_run.manifest_hash == reference.manifest_hash


# --------------------------------------------------------------------------
# Unavailable-core behavior
# --------------------------------------------------------------------------


def test_missing_core_reports_build_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_import(name: str) -> ModuleType:
        raise ImportError(f"no module named {name}")

    monkeypatch.setattr(rust_oracle, "import_module", raise_import)
    with pytest.raises(RustOracleUnavailable, match="uv sync"):
        load_rust_core()


def test_core_missing_registered_operations_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _EmptyCore:
        pass

    monkeypatch.setattr(rust_oracle, "import_module", lambda name: _EmptyCore())
    with pytest.raises(RustOracleUnavailable, match="smoke_roundtrip"):
        load_rust_core()


# --------------------------------------------------------------------------
# Actionable diff machinery
# --------------------------------------------------------------------------


def test_diff_reports_the_first_differing_byte_and_hex_context() -> None:
    report = diff_payloads(
        {"core/results.json": b"\x00\x01\x02\x03"},
        {"core/results.json": b"\x00\x01\xff\x03"},
    )
    assert not report.equal
    (difference,) = report.diffs
    assert difference.name == "core/results.json"
    assert difference.offset == 2
    assert difference.expected_length == 4
    assert difference.actual_length == 4
    assert difference.expected_excerpt == "02 03"
    assert difference.actual_excerpt == "ff 03"
    summary = report.summary()
    assert "core/results.json" in summary
    assert "offset 2" in summary
    assert "golden[2:]" in summary
    assert "actual[2:]" in summary


def test_diff_reports_missing_and_unexpected_artifacts() -> None:
    report = diff_payloads({"golden-only.bin": b"x"}, {"rust-only.bin": b"y"})
    assert report.artifact_names == ("golden-only.bin", "rust-only.bin")
    kinds = {difference.name: difference.kind for difference in report.diffs}
    assert kinds == {"golden-only.bin": "missing", "rust-only.bin": "unexpected"}
    summary = report.summary()
    assert "golden-only.bin" in summary
    assert "rust-only.bin" in summary


def test_diff_treats_equal_payloads_as_clean() -> None:
    report = diff_payloads({"a": b"abc", "b": b"def"}, {"b": b"def", "a": b"abc"})
    assert isinstance(report, ArtifactDiffReport)
    assert report.equal
    assert report.artifact_names == ()
    assert "byte-identical" in report.summary()


def test_oracle_error_carries_the_report() -> None:
    report = diff_payloads({"a": b"abc"}, {"a": b"abd"})
    with pytest.raises(RustOracleError) as captured:
        raise RustOracleError(report)
    assert captured.value.report is report
    assert "a" in str(captured.value)


# --------------------------------------------------------------------------
# Program and vector validation
# --------------------------------------------------------------------------


def test_deterministic_vector_pins_boundaries_and_length() -> None:
    vector = deterministic_vector(16, 7)
    assert vector == deterministic_vector(16, 7)
    assert len(vector) == 16
    assert vector[:3] == (0, 1, -1)
    assert all(-(2**63) <= value <= 2**63 - 1 for value in vector)
    assert vector != deterministic_vector(16, 8)


def test_program_rejects_unknown_operations() -> None:
    with pytest.raises(RustOracleConfigurationError):
        RustOracleOperation(op="does-not-exist", value=1)


def test_program_rejects_traversal_labels() -> None:
    with pytest.raises(RustOracleConfigurationError):
        RustOracleProgram(
            label="../escape",
            operations=(RustOracleOperation(op="smoke_roundtrip", value=1),),
        )


def test_golden_payloads_are_committed_for_the_resident_program() -> None:
    payloads = golden_payloads(DEFAULT_PROGRAM)
    assert set(payloads) == {"core/results.json", "core/manifest.json"}
    assert all(payload for payload in payloads.values())
