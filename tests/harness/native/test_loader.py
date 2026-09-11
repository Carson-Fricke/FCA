"""Loader-absence behavior: explicit dedicated errors, never silent fallback."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness._native import NativeCoreUnavailable, candidate_paths, load_native_core


def test_missing_library_raises_dedicated_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "harness._native.candidate_paths", lambda: (tmp_path / "does-not-exist.dll",)
    )

    with pytest.raises(NativeCoreUnavailable) as excinfo:
        load_native_core()

    assert "cargo build" in str(excinfo.value)
    assert "does-not-exist.dll" in str(excinfo.value)


def test_unloadable_library_raises_dedicated_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bogus = tmp_path / "not-a-library.txt"
    bogus.write_text("definitely not a shared library", encoding="ascii")
    monkeypatch.setattr("harness._native.candidate_paths", lambda: (bogus,))

    with pytest.raises(NativeCoreUnavailable) as excinfo:
        load_native_core()

    assert "could not be loaded" in str(excinfo.value)
    assert "cargo build" in str(excinfo.value)


def test_dedicated_error_is_not_a_bare_import_error() -> None:
    assert issubclass(NativeCoreUnavailable, RuntimeError)
    assert not issubclass(NativeCoreUnavailable, ImportError)
    assert not issubclass(NativeCoreUnavailable, OSError)


def test_environment_override_points_the_loader_at_one_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "custom-native.dll"
    monkeypatch.setenv("HARNESS_NATIVE_LIBRARY", str(override))

    assert candidate_paths() == (override,)

    with pytest.raises(NativeCoreUnavailable):
        load_native_core()


def test_default_candidates_search_the_rust_target() -> None:
    monkey_root = Path(__file__).resolve().parents[3]
    candidates = candidate_paths()
    assert candidates
    if sys.platform == "win32":
        assert any(
            "rust" in path.parts
            and "target" in path.parts
            and path.name == "harness_native.dll"
            for path in candidates
        )
    else:
        assert any(
            "rust" in path.parts
            and "target" in path.parts
            and "libharness_native" in path.name
            for path in candidates
        )
    assert all(str(path).startswith(str(monkey_root)) for path in candidates)
