"""Smoke test for the PyO3 bridge exposed as ``harness._rust``.

The extension module is compiled from ``rust/crates/harness-py`` by the
setuptools-rust build backend configured in ``setup.py``. It is additive: the
legacy ctypes loader in ``harness._native`` is unaffected and remains the only
supported path to the pinned Zig native core.
"""

from __future__ import annotations

from harness._rust import smoke_roundtrip


def test_smoke_roundtrip_round_trips_integers() -> None:
    for value in (0, 1, -1, 42, -7, 2**62):
        assert smoke_roundtrip(value) == value


def test_smoke_roundtrip_is_callable_from_python() -> None:
    assert smoke_roundtrip(123) == 123
