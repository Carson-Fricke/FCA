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
