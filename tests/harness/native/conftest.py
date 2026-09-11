"""Fixtures for the native-core seam tests.

The native library is a build artifact, not a Python dependency. Tests that
need it load it once per session; when the library has not been built yet the
tests skip with the loader's explicit reason so the default ``pytest`` run
stays green on a machine without a prior ``cargo build -p native``.
"""

from __future__ import annotations

import pytest

from harness._native import NativeCore, NativeCoreUnavailable, load_native_core


@pytest.fixture(scope="session")
def native_core() -> NativeCore:
    """Load the built native core, or skip with build instructions."""
    try:
        return load_native_core()
    except NativeCoreUnavailable as error:
        pytest.skip(f"native core library is not built: {error}")
