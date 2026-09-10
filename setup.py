"""Build extensions for the FCA Python distributions.

Package metadata and the pure-Python package layout live in ``pyproject.toml``;
this file exists only to attach the PyO3 extension module ``harness._rust``
built from ``rust/crates/harness-py`` via ``setuptools-rust``. Editable
installs and wheel builds therefore compile the bridge in place, while the
legacy ctypes loader in ``harness._native`` is untouched.
"""

from __future__ import annotations

from setuptools import setup
from setuptools_rust import Binding, RustExtension

setup(
    rust_extensions=[
        RustExtension(
            "harness._rust",
            path="rust/crates/harness-py/Cargo.toml",
            binding=Binding.PyO3,
            features=["extension-module"],
        )
    ],
    zip_safe=False,
)
