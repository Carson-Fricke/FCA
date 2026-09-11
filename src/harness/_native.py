"""Loader for the native harness core shared library.

The native core (``src/harness/rust/crates/harness-native``, built by Cargo)
exports a small, versioned C ABI. This module is the only supported way for
Python code to reach it. Loading is strict: if the library is missing, unloadable, or was
built against a different ABI version, :func:`load_native_core` raises
:class:`NativeCoreUnavailable` with build instructions. There is deliberately
no Python fallback and no bare ``ImportError`` — the harness treats a missing
native core as a hard configuration error.

Determinism: the loader only touches the local filesystem. It never consults
the clock, randomness, or the network.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Any, Final

#: ABI version this loader is pinned to. Must match ``types::ABI_VERSION`` in
#: ``src/harness/rust/crates/harness-native/src/types.rs``. Bump both together.
NATIVE_ABI_VERSION: Final = 2

#: Pinned fixed-point decimal scale of the native core. Matches the
#: repository's binary decimal convention ``pa.decimal128(38, 18)``.
DECIMAL_SCALE: Final = 18

_ERROR_NAMES: Final[dict[int, str]] = {
    0: "ok",
    1: "invalid_argument",
    2: "invalid_symbol",
    3: "buffer_too_small",
    4: "invalid_decimal",
    5: "invalid_timestamp",
    6: "out_of_range",
    7: "unknown",
}

_BUILD_HINT: Final = (
    "Run `cargo build -p harness-native` from the `src/harness/rust/` workspace to "
    "build the native core (outputs under `src/harness/rust/target/`), then retry."
)


class NativeCoreUnavailable(RuntimeError):
    """The native core library is absent, unloadable, or ABI-incompatible.

    This is a dedicated, explicit error. Callers must not catch it and fall
    back to Python; the native core is a required harness component.
    """


class NativeCoreError(RuntimeError):
    """A native core call completed but reported an error code."""


def _error_name(code: int) -> str:
    return _ERROR_NAMES.get(code, f"unknown_code_{code}")


def _library_names() -> tuple[str, ...]:
    """Return the platform-specific shared-library file names."""
    if sys.platform == "win32":
        return ("harness_native.dll", "libharness_native.dll")
    if sys.platform == "darwin":
        return ("libharness_native.dylib",)
    return ("libharness_native.so",)


def candidate_paths() -> tuple[Path, ...]:
    """Return every location the loader searches for the shared library.

    ``HARNESS_NATIVE_LIBRARY`` may point at one explicit shared-library file
    and takes precedence. Otherwise the loader looks in the Cargo target
    directories of the repository checkout: ``src/harness/rust/target/release``
    then ``src/harness/rust/target/debug``. There is no Zig fallback.
    """
    override = os.environ.get("HARNESS_NATIVE_LIBRARY")
    if override:
        return (Path(override),)
    repository_root = Path(__file__).resolve().parents[2]
    directories = (
        repository_root / "src" / "harness" / "rust" / "target" / "release",
        repository_root / "src" / "harness" / "rust" / "target" / "debug",
    )
    return tuple(
        directory / name for directory in directories for name in _library_names()
    )


def _bind(library: ctypes.CDLL, name: str) -> Any:
    try:
        return getattr(library, name)
    except AttributeError as error:
        raise NativeCoreUnavailable(
            f"Native core library does not export {name}; it was built from a "
            f"different source tree or is stale. {_BUILD_HINT}"
        ) from error


def _raise_for_code(code: int, operation: str) -> None:
    if code != 0:
        raise NativeCoreError(
            f"native core {operation} failed: {_error_name(code)} (code {code})"
        )


class NativeCore:
    """Bound, validated handle on the native core C ABI."""

    def __init__(self, library: ctypes.CDLL) -> None:
        self._abi_version_fn = _bind(library, "harness_native_abi_version")
        self._abi_version_fn.restype = ctypes.c_uint32
        self._abi_version_fn.argtypes = []
        reported = int(self._abi_version_fn())
        if reported != NATIVE_ABI_VERSION:
            raise NativeCoreUnavailable(
                f"Native core ABI version {reported} does not match the "
                f"required version {NATIVE_ABI_VERSION}; rebuild the library. {_BUILD_HINT}"
            )

        self._canonicalize = _bind(library, "harness_symbol_canonicalize")
        self._canonicalize.restype = ctypes.c_int
        self._canonicalize.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        self._derive_id = _bind(library, "harness_id_derive")
        self._derive_id.restype = ctypes.c_int
        self._derive_id.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_char),
        ]

        self._decimal_text = _bind(library, "harness_decimal_text")
        self._decimal_text.restype = ctypes.c_int
        self._decimal_text.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        self._trade_row = _bind(library, "harness_trade_row_serialize")
        self._trade_row.restype = ctypes.c_int
        self._trade_row.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_uint64,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        self._decimal_batch = _bind(library, "harness_decimal_batch_values")
        self._decimal_batch.restype = ctypes.c_int
        self._decimal_batch.argtypes = [
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        self._arrow_batch = _bind(library, "harness_arrow_decimal_batch")
        self._arrow_batch.restype = ctypes.c_int
        self._arrow_batch.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        double_p = ctypes.POINTER(ctypes.c_double)
        ubyte_p = ctypes.POINTER(ctypes.c_ubyte)
        uint32_p = ctypes.POINTER(ctypes.c_uint32)

        self._quant_max_steps = _bind(library, "harness_quant_max_steps")
        self._quant_max_steps.restype = ctypes.c_uint32
        self._quant_max_steps.argtypes = []

        self._quant_price_batch = _bind(library, "harness_quant_crr_price_batch")
        self._quant_price_batch.restype = ctypes.c_int
        self._quant_price_batch.argtypes = [
            double_p,
            double_p,
            double_p,
            double_p,
            double_p,
            double_p,
            ubyte_p,
            uint32_p,
            ctypes.c_size_t,
            double_p,
            double_p,
            double_p,
            double_p,
            double_p,
        ]

        self._quant_implied_time = _bind(
            library, "harness_quant_crr_implied_time_batch"
        )
        self._quant_implied_time.restype = ctypes.c_int
        self._quant_implied_time.argtypes = [
            double_p,
            double_p,
            double_p,
            double_p,
            double_p,
            ubyte_p,
            uint32_p,
            double_p,
            double_p,
            double_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            double_p,
        ]

        self._quant_implied_underlying = _bind(
            library, "harness_quant_crr_implied_underlying_batch"
        )
        self._quant_implied_underlying.restype = ctypes.c_int
        self._quant_implied_underlying.argtypes = [
            double_p,
            double_p,
            double_p,
            double_p,
            double_p,
            ubyte_p,
            uint32_p,
            double_p,
            double_p,
            double_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            double_p,
        ]

    @property
    def abi_version(self) -> int:
        """Return the ABI version reported by the loaded library."""
        return NATIVE_ABI_VERSION

    def symbol_canonicalize(self, value: str) -> str:
        """Canonicalize a market or OSI symbol exactly like the core."""
        encoded = value.encode("ascii")
        out = ctypes.create_string_buffer(len(encoded) + 1)
        written = ctypes.c_size_t(0)
        code = self._canonicalize(
            encoded, len(encoded), out, len(out), ctypes.byref(written)
        )
        _raise_for_code(code, "symbol canonicalization")
        return out.raw[: written.value].decode("ascii")

    def derive_id(self, seed: bytes, kind: str, ordinal: int) -> bytes:
        """Derive the deterministic 16-byte UUID for ``ordinal``."""
        if ordinal < 0 or ordinal >= 2**64:
            raise NativeCoreError(
                "native core id derivation failed: invalid_argument (ordinal out of range)"
            )
        out = ctypes.create_string_buffer(16)
        code = self._derive_id(
            seed, len(seed), kind.encode("ascii"), len(kind), ordinal, out
        )
        _raise_for_code(code, "id derivation")
        return out.raw[:16]

    def decimal_text(self, unscaled: int, scale: int = DECIMAL_SCALE) -> str:
        """Render an unscaled integer at ``scale`` as canonical decimal text."""
        packed = unscaled.to_bytes(16, "little", signed=True)
        out = ctypes.create_string_buffer(64)
        written = ctypes.c_size_t(0)
        code = self._decimal_text(packed, scale, out, len(out), ctypes.byref(written))
        _raise_for_code(code, "decimal rendering")
        return out.raw[: written.value].decode("ascii")

    def trade_row_json(
        self,
        *,
        event_id: str,
        timestamp_ns: int,
        sequence: int,
        symbol: str,
        price_unscaled: int,
        quantity_unscaled: int,
        venue: str | None,
        trade_id: str | None,
    ) -> bytes:
        """Serialize a canonical trade row to the canonical JSON bytes."""
        packed_timestamp = timestamp_ns.to_bytes(16, "little", signed=True)
        packed_price = price_unscaled.to_bytes(16, "little", signed=True)
        packed_quantity = quantity_unscaled.to_bytes(16, "little", signed=True)
        event_id_bytes = event_id.encode("utf-8")
        symbol_bytes = symbol.encode("utf-8")
        venue_bytes = None if venue is None else venue.encode("utf-8")
        trade_id_bytes = None if trade_id is None else trade_id.encode("utf-8")
        out = ctypes.create_string_buffer(1024)
        written = ctypes.c_size_t(0)
        code = self._trade_row(
            event_id_bytes,
            len(event_id_bytes),
            packed_timestamp,
            sequence,
            symbol_bytes,
            len(symbol_bytes),
            packed_price,
            packed_quantity,
            venue_bytes,
            0 if venue_bytes is None else len(venue_bytes),
            trade_id_bytes,
            0 if trade_id_bytes is None else len(trade_id_bytes),
            out,
            len(out),
            ctypes.byref(written),
        )
        _raise_for_code(code, "trade row serialization")
        return out.raw[: written.value]

    def decimal_batch_values(self) -> list[int]:
        """Return the unscaled values of the pinned Arrow decimal batch."""
        count = ctypes.c_size_t(0)
        code = self._decimal_batch(None, 0, ctypes.byref(count))
        _raise_for_code(code, "decimal batch size query")
        out = ctypes.create_string_buffer(count.value * 16)
        code = self._decimal_batch(out, count.value, ctypes.byref(count))
        _raise_for_code(code, "decimal batch read")
        return [
            int.from_bytes(
                out.raw[index * 16 : (index + 1) * 16], "little", signed=True
            )
            for index in range(count.value)
        ]

    def arrow_decimal_batch(self, schema_ptr: int, array_ptr: int) -> None:
        """Export the pinned decimal batch through the Arrow C Data Interface.

        ``schema_ptr`` and ``array_ptr`` are addresses of caller-allocated
        ``ArrowSchema``/``ArrowArray`` C structs. The caller must call each
        struct's ``release`` callback after importing.
        """
        code = self._arrow_batch(schema_ptr, array_ptr)
        _raise_for_code(code, "Arrow batch export")

    def quant_max_steps(self) -> int:
        """Return the largest binomial step count the quant kernels accept."""
        return int(self._quant_max_steps())

    def quant_crr_price_batch(
        self,
        s: Any,
        k: Any,
        t: Any,
        r: Any,
        q: Any,
        sigma: Any,
        flags: Any,
        steps: Any,
        count: int,
        out_price: Any,
        out_delta: Any,
        out_gamma: Any,
        out_theta: Any,
        out_vega: Any,
    ) -> None:
        """Batch CRR prices with tree greeks through the native core.

        Every argument is a ``ctypes`` pointer to ``count`` contiguous
        elements (``float64`` inputs and outputs, ``uint8`` flags, ``uint32``
        steps). Invalid elements arrive as NaN; only structural problems
        raise.
        """
        code = self._quant_price_batch(
            s,
            k,
            t,
            r,
            q,
            sigma,
            flags,
            steps,
            count,
            out_price,
            out_delta,
            out_gamma,
            out_theta,
            out_vega,
        )
        _raise_for_code(code, "quant CRR price batch")

    def quant_crr_implied_time_batch(
        self,
        s: Any,
        k: Any,
        r: Any,
        q: Any,
        sigma: Any,
        flags: Any,
        steps: Any,
        target: Any,
        t_lower: Any,
        t_upper: Any,
        count: int,
        bisect_iterations: int,
        out_t: Any,
    ) -> None:
        """Batch implied time to expiry through the native core.

        Solves the CRR price equation for time to expiry within
        ``[t_lower, t_upper]`` per element; elements whose bracket does not
        sign-change arrive as NaN.
        """
        code = self._quant_implied_time(
            s,
            k,
            r,
            q,
            sigma,
            flags,
            steps,
            target,
            t_lower,
            t_upper,
            count,
            bisect_iterations,
            out_t,
        )
        _raise_for_code(code, "quant CRR implied time batch")

    def quant_crr_implied_underlying_batch(
        self,
        k: Any,
        t: Any,
        r: Any,
        q: Any,
        sigma: Any,
        flags: Any,
        steps: Any,
        target: Any,
        s_lower: Any,
        s_upper: Any,
        count: int,
        bisect_iterations: int,
        out_s: Any,
    ) -> None:
        """Batch implied underlying spot through the native core.

        Solves the CRR price equation for the spot within
        ``[s_lower, s_upper]`` per element; elements whose bracket does not
        sign-change arrive as NaN.
        """
        code = self._quant_implied_underlying(
            k,
            t,
            r,
            q,
            sigma,
            flags,
            steps,
            target,
            s_lower,
            s_upper,
            count,
            bisect_iterations,
            out_s,
        )
        _raise_for_code(code, "quant CRR implied underlying batch")


def load_native_core(
    library_path: str | os.PathLike[str] | None = None,
) -> NativeCore:
    """Load and validate the native core shared library.

    Raises :class:`NativeCoreUnavailable` when the library is absent,
    unloadable, or built against a different ABI version.
    """
    if library_path is not None:
        candidates: tuple[Path, ...] = (Path(library_path),)
    else:
        candidates = candidate_paths()
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if not existing:
        raise NativeCoreUnavailable(
            "Native core library not found. Searched: "
            + ", ".join(str(candidate) for candidate in candidates)
            + f". {_BUILD_HINT}"
        )
    library_path_resolved = existing[0]
    try:
        library = ctypes.CDLL(str(library_path_resolved))
    except OSError as error:
        raise NativeCoreUnavailable(
            f"Native core library at {library_path_resolved} could not be "
            f"loaded: {error}. {_BUILD_HINT}"
        ) from error
    return NativeCore(library)


__all__ = [
    "DECIMAL_SCALE",
    "NATIVE_ABI_VERSION",
    "NativeCore",
    "NativeCoreError",
    "NativeCoreUnavailable",
    "candidate_paths",
    "load_native_core",
]
