"""End-to-end Arrow C Data Interface bridge: Zig export, pyarrow import."""

from __future__ import annotations

import ctypes
import gc
from decimal import Decimal

import pyarrow as pa

from harness._native import DECIMAL_SCALE, NativeCore

_ARROW_DECIMAL_TYPE = pa.decimal128(38, 18)


class ArrowSchema(ctypes.Structure):
    """Mirror of the Arrow C Data Interface ArrowSchema struct."""

    _fields_ = [
        ("format", ctypes.c_char_p),
        ("name", ctypes.c_char_p),
        ("metadata", ctypes.c_void_p),
        ("flags", ctypes.c_int64),
        ("n_children", ctypes.c_int64),
        ("children", ctypes.c_void_p),
        ("dictionary", ctypes.c_void_p),
        ("release", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
        ("private_data", ctypes.c_void_p),
    ]


class ArrowArray(ctypes.Structure):
    """Mirror of the Arrow C Data Interface ArrowArray struct."""

    _fields_ = [
        ("length", ctypes.c_int64),
        ("null_count", ctypes.c_int64),
        ("offset", ctypes.c_int64),
        ("n_buffers", ctypes.c_int64),
        ("n_children", ctypes.c_int64),
        ("buffers", ctypes.POINTER(ctypes.c_void_p)),
        ("children", ctypes.c_void_p),
        ("dictionary", ctypes.c_void_p),
        ("release", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
        ("private_data", ctypes.c_void_p),
    ]


def _release_if_owned(struct: ArrowSchema | ArrowArray) -> None:
    """Release the C struct only if pyarrow did not take ownership."""
    callback = struct.release
    if callback:
        callback(ctypes.addressof(struct))


def test_native_decimal_batch_imports_into_pyarrow(native_core: NativeCore) -> None:
    schema = ArrowSchema()
    array = ArrowArray()
    native_core.arrow_decimal_batch(ctypes.addressof(schema), ctypes.addressof(array))

    assert schema.format == b"d:38,18"
    assert schema.n_children == 0
    assert array.length > 0
    assert array.null_count == 0
    assert array.n_buffers == 2
    assert array.release

    try:
        # pyarrow takes ownership of both C structs on a successful import and
        # invokes their release callbacks when the imported values die.
        values = pa.Array._import_from_c(
            ctypes.addressof(array), ctypes.addressof(schema)
        )
    except Exception:
        _release_if_owned(schema)
        _release_if_owned(array)
        raise

    assert values.type == _ARROW_DECIMAL_TYPE
    unscaled_values = native_core.decimal_batch_values()
    expected = [Decimal(value).scaleb(-DECIMAL_SCALE) for value in unscaled_values]
    assert values.to_pylist() == expected
    assert array.length == len(expected)

    # Tie the imported binary values back to the canonical decimal text.
    for unscaled, imported in zip(unscaled_values, values.to_pylist(), strict=True):
        assert native_core.decimal_text(unscaled) == str(imported)


def test_imported_batch_survives_collection(native_core: NativeCore) -> None:
    schema = ArrowSchema()
    array = ArrowArray()
    native_core.arrow_decimal_batch(ctypes.addressof(schema), ctypes.addressof(array))

    values = pa.Array._import_from_c(ctypes.addressof(array), ctypes.addressof(schema))
    snapshot = values.to_pylist()
    del values
    gc.collect()

    assert snapshot == [
        Decimal(value).scaleb(-DECIMAL_SCALE)
        for value in native_core.decimal_batch_values()
    ]
