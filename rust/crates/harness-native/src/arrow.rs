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

/// The pinned deterministic batch of unscaled decimal128(38, 18) values. This is
/// the golden vector carried over from the retired Zig native core; the Python
/// parity test reads it back through `harness_decimal_batch_values` and asserts
/// the imported pyarrow values byte-for-byte.
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

#[cfg(test)]
mod tests {
    use core::ffi::CStr;

    use arrow_array::{Array, Decimal128Array};
    use arrow_schema::{DataType, Field};

    use super::{
        export_decimal_batch, ArrowArray, ArrowSchema, BATCH_LEN, BATCH_NAME, BATCH_VALUES,
        DECIMAL_PRECISION, DECIMAL_SCALE,
    };

    fn zeroed_schema() -> ArrowSchema {
        unsafe { core::mem::zeroed() }
    }

    fn zeroed_array() -> ArrowArray {
        unsafe { core::mem::zeroed() }
    }

    fn release(schema: &mut ArrowSchema, array: &mut ArrowArray) {
        unsafe {
            if let Some(callback) = schema.release {
                callback(schema);
            }
            if let Some(callback) = array.release {
                callback(array);
            }
        }
    }

    #[test]
    fn exported_batch_carries_the_pinned_golden_artifacts() {
        let mut schema = zeroed_schema();
        let mut array = zeroed_array();
        assert!(unsafe { export_decimal_batch(&mut schema, &mut array) });

        let format = unsafe { CStr::from_ptr(schema.format) };
        assert_eq!(format.to_bytes(), b"d:38,18");
        let name = unsafe { CStr::from_ptr(schema.name) };
        assert_eq!(name.to_bytes(), BATCH_NAME.as_bytes());
        assert_eq!(schema.n_children, 0);
        assert_eq!(schema.flags, 0);

        assert_eq!(array.length, BATCH_LEN as i64);
        assert_eq!(array.null_count, 0);
        assert_eq!(array.offset, 0);
        assert_eq!(array.n_buffers, 2);
        assert_eq!(array.n_children, 0);
        assert!(schema.release.is_some());
        assert!(array.release.is_some());

        let buffers = unsafe { core::slice::from_raw_parts(array.buffers, 2) };
        assert!(buffers[0].is_null());
        for (index, expected) in BATCH_VALUES.iter().enumerate() {
            let mut bytes = [0u8; 16];
            unsafe {
                core::ptr::copy_nonoverlapping(
                    buffers[1].cast::<u8>().add(index * 16),
                    bytes.as_mut_ptr(),
                    16,
                );
            }
            assert_eq!(i128::from_le_bytes(bytes), *expected);
        }

        release(&mut schema, &mut array);
        assert!(schema.release.is_none());
        assert!(array.release.is_none());

        // Zeroed structs no longer own anything, so dropping them is a no-op.
    }

    #[test]
    fn exported_batch_round_trips_through_arrow_rs() {
        let mut schema = zeroed_schema();
        let mut array = zeroed_array();
        assert!(unsafe { export_decimal_batch(&mut schema, &mut array) });

        // Take ownership of the array struct and import it with arrow-rs, which
        // is the same path pyarrow follows on the Python side.
        let imported = unsafe {
            let owned = core::ptr::read(&array);
            arrow::ffi::from_ffi(owned, &schema).expect("imported arrow batch")
        };
        let values = Decimal128Array::from(imported);
        assert_eq!(values.len(), BATCH_LEN);
        assert_eq!(
            values.data_type(),
            &DataType::Decimal128(DECIMAL_PRECISION, DECIMAL_SCALE)
        );
        let round_tripped: Vec<i128> = values.values().to_vec();
        assert_eq!(round_tripped.as_slice(), BATCH_VALUES.as_slice());

        // The schema was not consumed by `from_ffi`; release it explicitly.
        unsafe {
            if let Some(callback) = schema.release {
                callback(&mut schema);
            }
        }
        assert!(schema.release.is_none());
        // `from_ffi` consumed the array and freed it on drop; the local struct
        // still has a stale release pointer, so null it before it drops.
        array.release = None;
    }

    #[test]
    fn schema_describes_the_pinned_decimal_type() {
        let field = Field::new(
            BATCH_NAME,
            DataType::Decimal128(DECIMAL_PRECISION, DECIMAL_SCALE),
            false,
        );
        let mut schema = ArrowSchema::try_from(&field).expect("pinned decimal schema");
        assert_eq!(schema.format().to_string(), "d:38,18");
        assert_eq!(schema.name(), Some(BATCH_NAME));
        unsafe {
            if let Some(callback) = schema.release {
                callback(&mut schema);
            }
        }
        assert!(schema.release.is_none());
    }
}
