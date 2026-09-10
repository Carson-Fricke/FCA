//! Arrow C Data Interface bridge for the native harness core.
//!
//! Reimplements the Arrow C struct definitions (`ArrowSchema`/`ArrowArray`) per
//! the stable `arrow/c/abi.h` layout and exports a deterministic batch of
//! fixed-point decimals at the pinned scale so Python can consume it through
//! pyarrow's documented ctypes import path (`pa.Array._import_from_c`). This is
//! the same bridge `arrow-rs`'s `arrow::ffi` produces; it is written here
//! directly so the native core stays free of a runtime Arrow dependency.
//!
//! Memory ownership follows the Arrow C Data Interface contract: the producer
//! allocates the schema metadata and value buffers, and the consumer releases
//! both structs exactly once by invoking their `release` callbacks, which free
//! everything the producer allocated. Release callbacks are idempotent.

use core::ffi::{c_char, c_void};

/// The Arrow C Data Interface schema descriptor (`arrow/c/abi.h` layout).
#[repr(C)]
pub struct ArrowSchema {
    /// Arrow format string (`d:38,18`).
    pub format: *const c_char,
    /// Optional field name.
    pub name: *const c_char,
    /// Optional key-value metadata.
    pub metadata: *const c_char,
    /// Arrow schema flags.
    pub flags: i64,
    /// Number of children.
    pub n_children: i64,
    /// Child schemas.
    pub children: *mut *mut ArrowSchema,
    /// Dictionary schema.
    pub dictionary: *mut ArrowSchema,
    /// Release callback.
    pub release: Option<unsafe extern "C" fn(*mut ArrowSchema)>,
    /// Producer-private data.
    pub private_data: *mut c_void,
}

/// The Arrow C Data Interface array descriptor (`arrow/c/abi.h` layout).
#[repr(C)]
pub struct ArrowArray {
    /// Logical length.
    pub length: i64,
    /// Null count.
    pub null_count: i64,
    /// Logical offset.
    pub offset: i64,
    /// Number of buffers.
    pub n_buffers: i64,
    /// Number of children.
    pub n_children: i64,
    /// Buffer pointers.
    pub buffers: *mut *const u8,
    /// Child arrays.
    pub children: *mut *mut ArrowArray,
    /// Dictionary array.
    pub dictionary: *mut ArrowArray,
    /// Release callback.
    pub release: Option<unsafe extern "C" fn(*mut ArrowArray)>,
    /// Producer-private data.
    pub private_data: *mut c_void,
}

/// The pinned Arrow type of the exported batch: decimal128 at the repository's
/// binary decimal precision and scale.
pub const FORMAT: &[u8] = b"d:38,18\0";
/// The exported field name.
pub const NAME: &[u8] = b"value\0";
/// The exported batch length.
pub const BATCH_LEN: usize = 8;

/// The pinned deterministic batch of unscaled decimal128(38, 18) values.
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

struct SchemaPrivate;

struct ArrayPrivate {
    #[allow(dead_code)]
    values: Box<[i128; BATCH_LEN]>,
    buffers: [*const u8; 2],
}

fn empty_schema() -> ArrowSchema {
    ArrowSchema {
        format: FORMAT.as_ptr().cast::<c_char>(),
        name: NAME.as_ptr().cast::<c_char>(),
        metadata: core::ptr::null(),
        flags: 0,
        n_children: 0,
        children: core::ptr::null_mut(),
        dictionary: core::ptr::null_mut(),
        release: None,
        private_data: core::ptr::null_mut(),
    }
}

/// Fill `out_schema` and `out_array` with the pinned decimal batch. The value
/// buffer is heap-allocated with `i128` alignment; both structs are only
/// written once every allocation has succeeded.
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
    let schema_private = Box::new(SchemaPrivate);
    let values = Box::new(BATCH_VALUES);
    let mut array_private = Box::new(ArrayPrivate {
        values,
        buffers: [core::ptr::null(), core::ptr::null()],
    });
    array_private.buffers[1] = array_private.values.as_ptr().cast::<u8>();

    let schema_private_ptr = Box::into_raw(schema_private).cast::<c_void>();
    let array_private_ptr = Box::into_raw(array_private);

    unsafe {
        *out_schema = ArrowSchema {
            release: Some(release_schema),
            private_data: schema_private_ptr,
            ..empty_schema()
        };
        *out_array = ArrowArray {
            length: BATCH_LEN as i64,
            null_count: 0,
            offset: 0,
            n_buffers: 2,
            n_children: 0,
            buffers: (*array_private_ptr).buffers.as_mut_ptr(),
            children: core::ptr::null_mut(),
            dictionary: core::ptr::null_mut(),
            release: Some(release_array),
            private_data: array_private_ptr.cast::<c_void>(),
        };
    }
    true
}

unsafe extern "C" fn release_schema(schema: *mut ArrowSchema) {
    if schema.is_null() {
        return;
    }
    let schema_ref = unsafe { &mut *schema };
    if schema_ref.private_data.is_null() {
        return;
    }
    drop(unsafe { Box::from_raw(schema_ref.private_data.cast::<SchemaPrivate>()) });
    schema_ref.release = None;
    schema_ref.private_data = core::ptr::null_mut();
}

unsafe extern "C" fn release_array(array: *mut ArrowArray) {
    if array.is_null() {
        return;
    }
    let array_ref = unsafe { &mut *array };
    if array_ref.private_data.is_null() {
        return;
    }
    drop(unsafe { Box::from_raw(array_ref.private_data.cast::<ArrayPrivate>()) });
    array_ref.release = None;
    array_ref.private_data = core::ptr::null_mut();
}

#[cfg(test)]
mod tests {
    use super::{export_decimal_batch, ArrowArray, ArrowSchema, BATCH_LEN, BATCH_VALUES};

    #[test]
    fn exported_batch_carries_the_pinned_schema_and_values() {
        let mut schema = ArrowSchema {
            format: core::ptr::null(),
            name: core::ptr::null(),
            metadata: core::ptr::null(),
            flags: 0,
            n_children: 0,
            children: core::ptr::null_mut(),
            dictionary: core::ptr::null_mut(),
            release: None,
            private_data: core::ptr::null_mut(),
        };
        let mut array = ArrowArray {
            length: 0,
            null_count: 0,
            offset: 0,
            n_buffers: 0,
            n_children: 0,
            buffers: core::ptr::null_mut(),
            children: core::ptr::null_mut(),
            dictionary: core::ptr::null_mut(),
            release: None,
            private_data: core::ptr::null_mut(),
        };
        assert!(unsafe { export_decimal_batch(&mut schema, &mut array) });

        let format = unsafe { core::ffi::CStr::from_ptr(schema.format) };
        assert_eq!(format.to_bytes(), b"d:38,18");
        assert_eq!(array.length, BATCH_LEN as i64);
        assert_eq!(array.n_buffers, 2);
        assert!(schema.release.is_some());
        assert!(array.release.is_some());

        let buffers = unsafe { core::slice::from_raw_parts(array.buffers, 2) };
        assert!(buffers[0].is_null());
        for (index, expected) in BATCH_VALUES.iter().enumerate() {
            let mut bytes = [0u8; 16];
            unsafe {
                core::ptr::copy_nonoverlapping(
                    buffers[1].add(index * 16),
                    bytes.as_mut_ptr(),
                    16,
                );
            }
            assert_eq!(i128::from_le_bytes(bytes), *expected);
        }

        unsafe {
            (schema.release.unwrap())(&mut schema);
            (array.release.unwrap())(&mut array);
        }
        assert!(schema.release.is_none());
        assert!(array.release.is_none());
        assert!(schema.private_data.is_null());
        assert!(array.private_data.is_null());
    }
}
