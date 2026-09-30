//! Versioned Arrow schemas for point-in-time reference datasets.
//!
//! Ports `lake.reference.schemas` exactly: field names, types, order, and
//! nullability, plus the `lake.schema` / `lake.schema.version` metadata, must
//! stay byte-identical to the Python-written schemas because Python-written
//! fixture Parquet must decode against the Rust-written schema and
//! Rust-written Parquet must decode identically to the Python fixtures.
//!
//! Reference datasets do not use the market-data schema registry in
//! `lake_contracts::schemas` because their partitions are not per-symbol
//! sessions. Increment [`REFERENCE_SCHEMA_VERSION`] for any field, type,
//! order, nullability, or semantic change.

use std::sync::Arc;

use arrow_array::{
    Array, ArrayRef, BooleanArray, Date32Array, Decimal128Array, Int32Array, RecordBatch,
    StringArray, TimestampNanosecondArray, UInt64Array,
};
use arrow_schema::{DataType, Field, FieldRef, Schema, TimeUnit};
use arrow_select::concat::concat_batches;
use lake_contracts::{Date, DateTime, Decimal18, SCHEMA_METADATA_KEY, SCHEMA_VERSION_METADATA_KEY};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

use crate::error::ReferenceError;

/// The pinned reference schema version.
pub const REFERENCE_SCHEMA_VERSION: &str = "1";

fn utc_timestamp() -> DataType {
    DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()))
}

fn decimal() -> DataType {
    DataType::Decimal128(38, 18)
}

fn required(name: &str, data_type: DataType) -> Field {
    Field::new(name, data_type, false)
}

fn nullable(name: &str, data_type: DataType) -> Field {
    Field::new(name, data_type, true)
}

fn with_metadata(name: &str, fields: Vec<Field>) -> Schema {
    let mut metadata = std::collections::HashMap::new();
    metadata.insert(SCHEMA_METADATA_KEY.to_string(), name.to_string());
    metadata.insert(
        SCHEMA_VERSION_METADATA_KEY.to_string(),
        REFERENCE_SCHEMA_VERSION.to_string(),
    );
    Schema::new_with_metadata(fields, metadata)
}

/// The pinned `reference.classification` schema.
pub fn classification_schema() -> Schema {
    with_metadata(
        "reference.classification",
        vec![
            required("file_creation_time", utc_timestamp()),
            required("symbol", arrow_schema::DataType::Utf8),
            required("security_name", arrow_schema::DataType::Utf8),
            required("listing_exchange", arrow_schema::DataType::Utf8),
            required("market_category", arrow_schema::DataType::Utf8),
            required("etf", arrow_schema::DataType::Boolean),
            required("round_lot_size", decimal()),
            required("test_issue", arrow_schema::DataType::Boolean),
            nullable("financial_status", arrow_schema::DataType::Utf8),
        ],
    )
}

/// The pinned classification schema extended with the optional extras columns
/// the batch carries.
///
/// The pinned metadata is preserved; the extras are additive and do not bump
/// [`REFERENCE_SCHEMA_VERSION`] because snapshots without them stay readable
/// under the pinned schema.
fn classification_schema_with_extras(
    include_cik: bool,
    include_sic: bool,
    include_sic_industry: bool,
) -> Schema {
    if !include_cik && !include_sic && !include_sic_industry {
        return classification_schema();
    }
    let base = classification_schema();
    let mut fields: Vec<FieldRef> = base.fields().iter().cloned().collect();
    if include_cik {
        fields.push(Arc::new(nullable("cik", DataType::UInt64)));
    }
    if include_sic {
        fields.push(Arc::new(nullable("sic", DataType::Utf8)));
    }
    if include_sic_industry {
        fields.push(Arc::new(nullable("sic_industry", DataType::Utf8)));
    }
    Schema::new_with_metadata(fields, base.metadata().clone())
}

/// The pinned `reference.fundamentals` schema.
pub fn fundamentals_schema() -> Schema {
    with_metadata(
        "reference.fundamentals",
        vec![
            required("cik", arrow_schema::DataType::UInt64),
            required("taxonomy", arrow_schema::DataType::Utf8),
            required("tag", arrow_schema::DataType::Utf8),
            required("unit", arrow_schema::DataType::Utf8),
            required("period_end", arrow_schema::DataType::Date32),
            nullable("fy", arrow_schema::DataType::Int32),
            nullable("fp", arrow_schema::DataType::Utf8),
            required("form", arrow_schema::DataType::Utf8),
            required("accn", arrow_schema::DataType::Utf8),
            required("filed", arrow_schema::DataType::Date32),
            nullable("frame", arrow_schema::DataType::Utf8),
            required("val", decimal()),
        ],
    )
}

/// One normalized classification partition row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClassificationRow {
    /// The snapshot creation instant, UTC.
    pub file_creation_time: DateTime,
    /// The trading symbol.
    pub symbol: String,
    /// The full security name.
    pub security_name: String,
    /// The listing exchange code.
    pub listing_exchange: String,
    /// The market category code.
    pub market_category: String,
    /// Whether the security is an exchange-traded fund.
    pub etf: bool,
    /// The round lot size at lake decimal scale.
    pub round_lot_size: Decimal18,
    /// Whether the security is a test issue.
    pub test_issue: bool,
    /// The financial status code, when present.
    pub financial_status: Option<String>,
}

/// One normalized fundamentals partition row.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct FactRow {
    /// The SEC CIK of the filer.
    pub cik: u64,
    /// The XBRL taxonomy.
    pub taxonomy: String,
    /// The XBRL tag.
    pub tag: String,
    /// The unit of measure.
    pub unit: String,
    /// The period the fact covers.
    pub period_end: Date,
    /// The fiscal year, when declared.
    pub fy: Option<i32>,
    /// The fiscal period, when declared.
    pub fp: Option<String>,
    /// The form type of the filing.
    pub form: String,
    /// The accession number of the filing.
    pub accn: String,
    /// The knowledge date: when the fact became public.
    pub filed: Date,
    /// The SEC frame label, when present.
    pub frame: Option<String>,
    /// The exact fact value at lake decimal scale.
    pub val: Decimal18,
}

impl FactRow {
    /// Build one row from an XBRL fact, mirroring `_fact_row`.
    pub fn from_fact(fact: &crate::edgar::XbrlFact) -> Self {
        Self {
            cik: fact.cik,
            taxonomy: fact.taxonomy.clone(),
            tag: fact.tag.clone(),
            unit: fact.unit.clone(),
            period_end: fact.period_end,
            fy: fact.fy,
            fp: fact.fp.clone(),
            form: fact.form.clone(),
            accn: fact.accn.clone(),
            filed: fact.filed,
            frame: fact.frame.clone(),
            val: fact.val,
        }
    }

    /// The point-in-time recency key: `(filed, accn)`.
    pub fn recency(&self) -> (Date, &str) {
        (self.filed, self.accn.as_str())
    }
}

impl Ord for FactRow {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.cik
            .cmp(&other.cik)
            .then_with(|| self.taxonomy.cmp(&other.taxonomy))
            .then_with(|| self.tag.cmp(&other.tag))
            .then_with(|| self.unit.cmp(&other.unit))
            .then_with(|| self.period_end.cmp(&other.period_end))
            .then_with(|| self.filed.cmp(&other.filed))
            .then_with(|| self.accn.cmp(&other.accn))
            .then_with(|| cmp_option(&self.fy, &other.fy))
            .then_with(|| cmp_option(&self.fp, &other.fp))
            .then_with(|| self.form.cmp(&other.form))
            .then_with(|| cmp_option(&self.frame, &other.frame))
            .then_with(|| self.val.cmp(&other.val))
    }
}

impl PartialOrd for FactRow {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

/// Nullable columns need a total order that tolerates `None`: a missing value
/// sorts after every present value, matching Python's `(is_none, value)` key.
fn cmp_option<T: Ord>(left: &Option<T>, right: &Option<T>) -> std::cmp::Ordering {
    match (left, right) {
        (None, None) => std::cmp::Ordering::Equal,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (Some(_), None) => std::cmp::Ordering::Less,
        (Some(left), Some(right)) => left.cmp(right),
    }
}

/// Normalize classification rows into deterministic partition order.
///
/// Rows without extras are written against the pinned schema exactly, so this
/// stays byte-compatible with snapshots written before the CIK join landed.
pub fn classification_table(rows: Vec<ClassificationRow>) -> Result<RecordBatch, ReferenceError> {
    classification_table_with_extras(
        rows.into_iter()
            .map(|row| (row, ClassificationExtras::default()))
            .collect(),
    )
}

/// Normalize classification rows plus optional extras into partition order.
///
/// Python sorts by the `("symbol",)` key with a stable sort. Optional columns
/// are appended after the pinned fields in the fixed order `cik`, `sic`,
/// `sic_industry`, and only when at least one row carries the value, so an
/// all-absent batch is byte-identical to [`classification_table`].
pub fn classification_table_with_extras(
    rows: Vec<(ClassificationRow, ClassificationExtras)>,
) -> Result<RecordBatch, ReferenceError> {
    let mut rows = rows;
    rows.sort_by(|(left, _), (right, _)| left.symbol.cmp(&right.symbol));
    let include_cik = rows.iter().any(|(_, extras)| extras.cik.is_some());
    let include_sic = rows.iter().any(|(_, extras)| extras.sic.is_some());
    let include_sic_industry = rows.iter().any(|(_, extras)| extras.sic_industry.is_some());
    let schema = classification_schema_with_extras(include_cik, include_sic, include_sic_industry);
    let file_creation_time = TimestampNanosecondArray::from(
        rows.iter()
            .map(|(row, _)| row.file_creation_time.to_nanos())
            .collect::<Vec<i64>>(),
    )
    .with_timezone("UTC");
    let symbol = StringArray::from(
        rows.iter()
            .map(|(row, _)| row.symbol.as_str())
            .collect::<Vec<&str>>(),
    );
    let security_name = StringArray::from(
        rows.iter()
            .map(|(row, _)| row.security_name.as_str())
            .collect::<Vec<&str>>(),
    );
    let listing_exchange = StringArray::from(
        rows.iter()
            .map(|(row, _)| row.listing_exchange.as_str())
            .collect::<Vec<&str>>(),
    );
    let market_category = StringArray::from(
        rows.iter()
            .map(|(row, _)| row.market_category.as_str())
            .collect::<Vec<&str>>(),
    );
    let etf = BooleanArray::from(rows.iter().map(|(row, _)| row.etf).collect::<Vec<bool>>());
    let round_lot_size = Decimal128Array::from(
        rows.iter()
            .map(|(row, _)| row.round_lot_size.unscaled())
            .collect::<Vec<i128>>(),
    )
    .with_precision_and_scale(38, 18)
    .map_err(|error| ReferenceError::invalid(error.to_string()))?;
    let test_issue = BooleanArray::from(
        rows.iter()
            .map(|(row, _)| row.test_issue)
            .collect::<Vec<bool>>(),
    );
    let financial_status = StringArray::from(
        rows.iter()
            .map(|(row, _)| row.financial_status.clone())
            .collect::<Vec<Option<String>>>(),
    );

    let mut columns: Vec<ArrayRef> = vec![
        Arc::new(file_creation_time),
        Arc::new(symbol),
        Arc::new(security_name),
        Arc::new(listing_exchange),
        Arc::new(market_category),
        Arc::new(etf),
        Arc::new(round_lot_size),
        Arc::new(test_issue),
        Arc::new(financial_status),
    ];
    if include_cik {
        columns.push(Arc::new(UInt64Array::from(
            rows.iter()
                .map(|(_, extras)| extras.cik)
                .collect::<Vec<Option<u64>>>(),
        )));
    }
    if include_sic {
        columns.push(Arc::new(StringArray::from(
            rows.iter()
                .map(|(_, extras)| extras.sic.clone())
                .collect::<Vec<Option<String>>>(),
        )));
    }
    if include_sic_industry {
        columns.push(Arc::new(StringArray::from(
            rows.iter()
                .map(|(_, extras)| extras.sic_industry.clone())
                .collect::<Vec<Option<String>>>(),
        )));
    }

    let batch = RecordBatch::try_new(Arc::new(schema), columns)
        .map_err(|error| ReferenceError::invalid(error.to_string()))?;
    Ok(batch)
}

/// Normalize fundamentals rows into deterministic partition order.
///
/// Rows are sorted by the pinned key `(cik, taxonomy, tag, unit, period_end,
/// filed, accn, fy, fp, form, frame, val)` with nullable columns sorting
/// after every present value.
pub fn fundamentals_table(rows: Vec<FactRow>) -> Result<RecordBatch, ReferenceError> {
    let mut rows = rows;
    rows.sort();
    let schema = fundamentals_schema();
    let cik = UInt64Array::from(rows.iter().map(|row| row.cik).collect::<Vec<u64>>());
    let taxonomy = StringArray::from(
        rows.iter()
            .map(|row| row.taxonomy.as_str())
            .collect::<Vec<&str>>(),
    );
    let tag = StringArray::from(
        rows.iter()
            .map(|row| row.tag.as_str())
            .collect::<Vec<&str>>(),
    );
    let unit = StringArray::from(
        rows.iter()
            .map(|row| row.unit.as_str())
            .collect::<Vec<&str>>(),
    );
    let period_end = Date32Array::from(
        rows.iter()
            .map(|row| row.period_end.days())
            .collect::<Vec<i32>>(),
    );
    let fy = Int32Array::from(rows.iter().map(|row| row.fy).collect::<Vec<Option<i32>>>());
    let fp = StringArray::from(
        rows.iter()
            .map(|row| row.fp.clone())
            .collect::<Vec<Option<String>>>(),
    );
    let form = StringArray::from(
        rows.iter()
            .map(|row| row.form.as_str())
            .collect::<Vec<&str>>(),
    );
    let accn = StringArray::from(
        rows.iter()
            .map(|row| row.accn.as_str())
            .collect::<Vec<&str>>(),
    );
    let filed = Date32Array::from(
        rows.iter()
            .map(|row| row.filed.days())
            .collect::<Vec<i32>>(),
    );
    let frame = StringArray::from(
        rows.iter()
            .map(|row| row.frame.clone())
            .collect::<Vec<Option<String>>>(),
    );
    let val = Decimal128Array::from(
        rows.iter()
            .map(|row| row.val.unscaled())
            .collect::<Vec<i128>>(),
    )
    .with_precision_and_scale(38, 18)
    .map_err(|error| ReferenceError::invalid(error.to_string()))?;

    let batch = RecordBatch::try_new(
        Arc::new(schema),
        vec![
            Arc::new(cik),
            Arc::new(taxonomy),
            Arc::new(tag),
            Arc::new(unit),
            Arc::new(period_end),
            Arc::new(fy),
            Arc::new(fp),
            Arc::new(form),
            Arc::new(accn),
            Arc::new(filed),
            Arc::new(frame),
            Arc::new(val),
        ],
    )
    .map_err(|error| ReferenceError::invalid(error.to_string()))?;
    Ok(batch)
}

/// Whether two schemas carry the same fields, ignoring all metadata.
///
/// This is the `check_metadata=False` comparison Python's readers apply.
pub fn fields_equivalent(actual: &Schema, expected: &Schema) -> bool {
    actual.fields().len() == expected.fields().len()
        && actual
            .fields()
            .iter()
            .zip(expected.fields().iter())
            .all(|(actual, expected)| {
                actual.name() == expected.name()
                    && actual.data_type() == expected.data_type()
                    && actual.is_nullable() == expected.is_nullable()
            })
}

/// Optional symbol-level columns beyond the pinned classification schema.
///
/// Reference snapshots may carry the symbol-to-CIK mapping and SIC
/// classification used by the screener facts route. The pinned schema stays
/// authoritative: these columns are additive and named. The ingest writes
/// `cik` from the SEC ticker map; `sic` and `sic_industry` stay absent until a
/// bulk EDGAR source exists.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ClassificationExtras {
    /// The SEC CIK the symbol maps to, when the snapshot carries one.
    pub cik: Option<u64>,
    /// The SIC code, when the snapshot carries one.
    pub sic: Option<String>,
    /// The SIC industry label, when the snapshot carries one.
    pub sic_industry: Option<String>,
}

/// Read one classification object into normalized rows.
///
/// The payload must decode against the pinned schema (optionally extended with
/// the [`ClassificationExtras`] columns), otherwise the error carries
/// `{context} object has an unexpected schema: {path}`.
pub fn read_classification_rows(
    payload: &[u8],
    path: &str,
    context: &str,
) -> Result<Vec<ClassificationRow>, ReferenceError> {
    Ok(
        read_classification_rows_with_extras(payload, path, context)?
            .into_iter()
            .map(|(row, _extras)| row)
            .collect(),
    )
}

/// Read one classification object into normalized rows plus optional extras.
///
/// The optional `cik`, `sic`, and `sic_industry` columns may follow the pinned
/// fields in any order; unknown extra columns are rejected exactly like a
/// missing or mistyped pinned field.
pub fn read_classification_rows_with_extras(
    payload: &[u8],
    path: &str,
    context: &str,
) -> Result<Vec<(ClassificationRow, ClassificationExtras)>, ReferenceError> {
    let batch = decode_classification_batch(payload, path, context)?;
    let columns = batch.columns();
    let file_creation_time = columns[0]
        .as_any()
        .downcast_ref::<TimestampNanosecondArray>()
        .ok_or_else(schema_error(path, context))?;
    let symbol = columns[1]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let security_name = columns[2]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let listing_exchange = columns[3]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let market_category = columns[4]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let etf = columns[5]
        .as_any()
        .downcast_ref::<BooleanArray>()
        .ok_or_else(schema_error(path, context))?;
    let round_lot_size = columns[6]
        .as_any()
        .downcast_ref::<Decimal128Array>()
        .ok_or_else(schema_error(path, context))?;
    let test_issue = columns[7]
        .as_any()
        .downcast_ref::<BooleanArray>()
        .ok_or_else(schema_error(path, context))?;
    let financial_status = columns[8]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let cik = optional_column::<UInt64Array>(&batch, "cik")?;
    let sic = optional_column::<StringArray>(&batch, "sic")?;
    let sic_industry = optional_column::<StringArray>(&batch, "sic_industry")?;

    let rows = (0..batch.num_rows())
        .map(|index| {
            let row = ClassificationRow {
                file_creation_time: DateTime::from_nanos(file_creation_time.value(index)),
                symbol: symbol.value(index).to_string(),
                security_name: security_name.value(index).to_string(),
                listing_exchange: listing_exchange.value(index).to_string(),
                market_category: market_category.value(index).to_string(),
                etf: etf.value(index),
                round_lot_size: Decimal18::from_unscaled_unchecked(round_lot_size.value(index)),
                test_issue: test_issue.value(index),
                financial_status: optional_string(financial_status, index),
            };
            let extras = ClassificationExtras {
                cik: cik.and_then(|values| optional_u64(values, index)),
                sic: sic.and_then(|values| optional_string(values, index)),
                sic_industry: sic_industry.and_then(|values| optional_string(values, index)),
            };
            (row, extras)
        })
        .collect();
    Ok(rows)
}

/// Read one fundamentals object into normalized rows.
///
/// The payload must decode against the pinned schema, otherwise the error
/// carries `{context} object has an unexpected schema: {path}`.
pub fn read_fundamentals_rows(
    payload: &[u8],
    path: &str,
    context: &str,
) -> Result<Vec<FactRow>, ReferenceError> {
    let batch = decode_batch(payload, path, context, &fundamentals_schema())?;
    let columns = batch.columns();
    let cik = columns[0]
        .as_any()
        .downcast_ref::<UInt64Array>()
        .ok_or_else(schema_error(path, context))?;
    let taxonomy = columns[1]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let tag = columns[2]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let unit = columns[3]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let period_end = columns[4]
        .as_any()
        .downcast_ref::<Date32Array>()
        .ok_or_else(schema_error(path, context))?;
    let fy = columns[5]
        .as_any()
        .downcast_ref::<Int32Array>()
        .ok_or_else(schema_error(path, context))?;
    let fp = columns[6]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let form = columns[7]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let accn = columns[8]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let filed = columns[9]
        .as_any()
        .downcast_ref::<Date32Array>()
        .ok_or_else(schema_error(path, context))?;
    let frame = columns[10]
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(schema_error(path, context))?;
    let val = columns[11]
        .as_any()
        .downcast_ref::<Decimal128Array>()
        .ok_or_else(schema_error(path, context))?;

    let rows = (0..batch.num_rows())
        .map(|index| FactRow {
            cik: cik.value(index),
            taxonomy: taxonomy.value(index).to_string(),
            tag: tag.value(index).to_string(),
            unit: unit.value(index).to_string(),
            period_end: Date::from_days(period_end.value(index)),
            fy: nullable_i32(fy, index),
            fp: optional_string(fp, index),
            form: form.value(index).to_string(),
            accn: accn.value(index).to_string(),
            filed: Date::from_days(filed.value(index)),
            frame: optional_string(frame, index),
            val: Decimal18::from_unscaled_unchecked(val.value(index)),
        })
        .collect();
    Ok(rows)
}

/// Decode one classification object, tolerating the optional extras columns.
fn decode_classification_batch(
    payload: &[u8],
    path: &str,
    context: &str,
) -> Result<RecordBatch, ReferenceError> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(bytes::Bytes::copy_from_slice(payload))
        .map_err(|error| {
            ReferenceError::invalid(format!("reference object is not readable: {error}"))
        })?;
    let expected = classification_schema();
    if !classification_fields_compatible(builder.schema().as_ref(), &expected) {
        return Err(ReferenceError::Schema(format!(
            "{context} object has an unexpected schema: {path}"
        )));
    }
    let reader = builder.build().map_err(|error| {
        ReferenceError::invalid(format!("reference object is not readable: {error}"))
    })?;
    let mut rows: Option<RecordBatch> = None;
    for batch in reader {
        let batch = batch.map_err(|error| {
            ReferenceError::invalid(format!("reference object is not readable: {error}"))
        })?;
        rows = Some(match rows {
            None => batch,
            Some(previous) => concat_batches(&previous.schema(), &[previous, batch])
                .map_err(|error| ReferenceError::invalid(error.to_string()))?,
        });
    }
    Ok(rows.unwrap_or_else(|| RecordBatch::new_empty(Arc::new(expected))))
}

/// Whether a file schema is the pinned classification schema plus optional extras.
fn classification_fields_compatible(actual: &Schema, expected: &Schema) -> bool {
    let pinned = expected.fields().len();
    if actual.fields().len() < pinned {
        return false;
    }
    let pinned_matches = actual
        .fields()
        .iter()
        .take(pinned)
        .zip(expected.fields().iter())
        .all(|(actual, expected)| {
            actual.name() == expected.name()
                && actual.data_type() == expected.data_type()
                && actual.is_nullable() == expected.is_nullable()
        });
    if !pinned_matches {
        return false;
    }
    let mut seen: Vec<&str> = Vec::new();
    for field in &actual.fields()[pinned..] {
        let supported = match field.name().as_str() {
            "cik" => field.data_type() == &DataType::UInt64,
            "sic" | "sic_industry" => field.data_type() == &DataType::Utf8,
            _ => false,
        };
        if !supported || seen.contains(&field.name().as_str()) {
            return false;
        }
        seen.push(field.name());
    }
    true
}

/// Borrow one optional named column of a decoded batch.
fn optional_column<'a, T: 'static>(
    batch: &'a RecordBatch,
    name: &str,
) -> Result<Option<&'a T>, ReferenceError> {
    match batch.column_by_name(name) {
        None => Ok(None),
        Some(column) => column
            .as_any()
            .downcast_ref::<T>()
            .map(Some)
            .ok_or_else(|| ReferenceError::Schema(format!("column {name} has an unexpected type"))),
    }
}

fn decode_batch(
    payload: &[u8],
    path: &str,
    context: &str,
    expected: &Schema,
) -> Result<RecordBatch, ReferenceError> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(bytes::Bytes::copy_from_slice(payload))
        .map_err(|error| {
            ReferenceError::invalid(format!("reference object is not readable: {error}"))
        })?;
    if !fields_equivalent(builder.schema().as_ref(), expected) {
        return Err(ReferenceError::Schema(format!(
            "{context} object has an unexpected schema: {path}"
        )));
    }
    let reader = builder.build().map_err(|error| {
        ReferenceError::invalid(format!("reference object is not readable: {error}"))
    })?;
    let mut rows: Option<RecordBatch> = None;
    for batch in reader {
        let batch = batch.map_err(|error| {
            ReferenceError::invalid(format!("reference object is not readable: {error}"))
        })?;
        rows = Some(match rows {
            None => batch,
            Some(previous) => concat_batches(&previous.schema(), &[previous, batch])
                .map_err(|error| ReferenceError::invalid(error.to_string()))?,
        });
    }
    Ok(rows.unwrap_or_else(|| RecordBatch::new_empty(Arc::new(expected.clone()))))
}

fn schema_error<'a>(path: &'a str, context: &'a str) -> impl FnOnce() -> ReferenceError + 'a {
    move || ReferenceError::Schema(format!("{context} object has an unexpected schema: {path}"))
}

fn optional_string(array: &StringArray, index: usize) -> Option<String> {
    if array.is_null(index) {
        None
    } else {
        Some(array.value(index).to_string())
    }
}

fn optional_u64(array: &UInt64Array, index: usize) -> Option<u64> {
    if array.is_null(index) {
        None
    } else {
        Some(array.value(index))
    }
}

fn nullable_i32(array: &Int32Array, index: usize) -> Option<i32> {
    if array.is_null(index) {
        None
    } else {
        Some(array.value(index))
    }
}
