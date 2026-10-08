//! Arrow C Data Interface bridge for the native harness core.
//!
//! The bridge is built on arrow-rs's C Data Interface support: the pinned
//! deterministic batch of fixed-point decimals is produced with
//! [`Decimal128Array`] at the repository's binary decimal precision and scale,
//! then exported through arrow-rs's ABI-compatible structs
//! ([`ArrowSchema`]/[`ArrowArray`], re-exported from `arrow::ffi`) so Python can
//! consume it through pyarrow's documented ctypes import path
//! (`pa.Array._import_from_c`). The previous hand-rolled `ArrowSchema`/
//! `ArrowArray` definitions are gone; the public names, the C struct layout, and
//! the exported C ABI symbols (`harness_arrow_decimal_batch`,
//! `harness_decimal_batch_values`) are unchanged.
//!
//! Memory ownership follows the Arrow C Data Interface contract: the producer
//! allocates the schema metadata and value buffers, and the consumer releases
//! both structs exactly once by invoking their `release` callbacks, which free
//! everything the producer allocated. arrow-rs installs and owns those
//! callbacks, so the release semantics stay identical to the rest of the
//! ecosystem.

use arrow_array::{Array, Decimal128Array};
use arrow_schema::{DataType, Field};

/// The Arrow C Data Interface array descriptor (`arrow/c/abi.h` layout).
pub use arrow::ffi::FFI_ArrowArray as ArrowArray;
/// The Arrow C Data Interface schema descriptor (`arrow/c/abi.h` layout).
pub use arrow::ffi::FFI_ArrowSchema as ArrowSchema;

/// Precision of the exported type, `decimal128(38, 18)`.
pub const DECIMAL_PRECISION: u8 = 38;
/// Scale of the exported type, the repository's binary decimal scale.
pub const DECIMAL_SCALE: i8 = 18;
/// The exported field name.
pub const BATCH_NAME: &str = "value";
/// The exported batch length.
pub const BATCH_LEN: usize = 8;

/// The pinned deterministic batch of unscaled decimal128(38, 18) values. The
/// Python parity test reads it back through `harness_decimal_batch_values` and
/// asserts the imported pyarrow values byte-for-byte.
pub const BATCH_VALUES: [i128; BATCH_LEN] = [
    1_500_000_000_000_000_000,
    1,
    0,
    -1_502_500_000_000_000_000,
    99_999_999_999_999_999_999,
    -99_999_999_999_999_999_999,
    12_345_678_901_234_567_890,
    15_025,
];

/// Build the pinned decimal array with arrow-rs. Returns `None` only if the
/// pinned values do not fit the pinned type, which is a compile-time invariant.
fn pinned_decimal_array() -> Option<Decimal128Array> {
    Decimal128Array::from_iter_values(BATCH_VALUES)
        .with_precision_and_scale(DECIMAL_PRECISION, DECIMAL_SCALE)
        .ok()
}

/// Fill `out_schema` and `out_array` with the pinned decimal batch. Both structs
/// are built by arrow-rs and then moved into the caller's storage by value, so
/// the release callbacks and private data transfer together and the caller owns
/// them exactly as the Arrow C Data Interface requires.
///
/// # Safety
///
/// `out_schema` and `out_array` must point to writable `ArrowSchema` /
/// `ArrowArray` storage that the consumer has not yet imported.
pub unsafe fn export_decimal_batch(
    out_schema: *mut ArrowSchema,
    out_array: *mut ArrowArray,
) -> bool {
    if out_schema.is_null() || out_array.is_null() {
        return false;
    }
    let Some(array) = pinned_decimal_array() else {
        return false;
    };
    let field = Field::new(
        BATCH_NAME,
        DataType::Decimal128(DECIMAL_PRECISION, DECIMAL_SCALE),
        false,
    );
    let Ok(schema) = ArrowSchema::try_from(&field) else {
        return false;
    };
    let ffi_array = ArrowArray::new(&array.into_data());

    // Ownership moves into the caller's storage; arrow-rs's release callbacks
    // free the buffers exactly once.
    unsafe {
        core::ptr::write(out_schema, schema);
        core::ptr::write(out_array, ffi_array);
    }
    true
}
