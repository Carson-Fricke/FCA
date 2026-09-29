use lake_contracts::{Date, DateTime, Decimal18, SCHEMA_METADATA_KEY, SCHEMA_VERSION_METADATA_KEY};

use super::*;
use crate::nasdaq::NasdaqSecurity;

fn eastern_instant() -> DateTime {
    DateTime::from_ymd_hms(2024, 4, 19, 21, 20, 0, 0).expect("utc")
}

fn classification_rows() -> Vec<ClassificationRow> {
    vec![
        ClassificationRow {
            file_creation_time: eastern_instant(),
            symbol: "MSFT".to_string(),
            security_name: "Microsoft Corporation Common Stock".to_string(),
            listing_exchange: "Q".to_string(),
            market_category: "Q".to_string(),
            etf: false,
            round_lot_size: Decimal18::parse("100").expect("lot"),
            test_issue: false,
            financial_status: Some("Q".to_string()),
        },
        ClassificationRow {
            file_creation_time: eastern_instant(),
            symbol: "AAPL".to_string(),
            security_name: "Apple Inc. Common Stock".to_string(),
            listing_exchange: "Q".to_string(),
            market_category: "Q".to_string(),
            etf: false,
            round_lot_size: Decimal18::parse("100").expect("lot"),
            test_issue: false,
            financial_status: None,
        },
    ]
}

fn fundamentals_rows() -> Vec<FactRow> {
    vec![
        FactRow {
            cik: 320193,
            taxonomy: "us-gaap".to_string(),
            tag: "Revenues".to_string(),
            unit: "USD".to_string(),
            period_end: Date::from_ymd(2023, 12, 30).expect("date"),
            fy: Some(2024),
            fp: Some("Q1".to_string()),
            form: "10-Q".to_string(),
            accn: "0000320193-24-000006".to_string(),
            filed: Date::from_ymd(2024, 2, 2).expect("date"),
            frame: Some("CY2023Q4".to_string()),
            val: Decimal18::parse("119575000000").expect("val"),
        },
        FactRow {
            cik: 320193,
            taxonomy: "dei".to_string(),
            tag: "EntityCommonStockSharesOutstanding".to_string(),
            unit: "shares".to_string(),
            period_end: Date::from_ymd(2023, 10, 20).expect("date"),
            fy: None,
            fp: None,
            form: "10-K".to_string(),
            accn: "0000320193-23-000106".to_string(),
            filed: Date::from_ymd(2023, 11, 3).expect("date"),
            frame: None,
            val: Decimal18::parse("15464661000").expect("val"),
        },
    ]
}

fn column_names(schema: &Schema) -> Vec<(&str, bool)> {
    schema
        .fields()
        .iter()
        .map(|field| (field.name().as_str(), field.is_nullable()))
        .collect()
}

#[test]
fn reference_schemas_are_versioned() {
    for schema in [classification_schema(), fundamentals_schema()] {
        assert!(schema.metadata().contains_key(SCHEMA_METADATA_KEY));
        assert_eq!(
            schema.metadata().get(SCHEMA_VERSION_METADATA_KEY),
            Some(&REFERENCE_SCHEMA_VERSION.to_string())
        );
    }
    assert_eq!(
        classification_schema().metadata().get(SCHEMA_METADATA_KEY),
        Some(&"reference.classification".to_string())
    );
    assert_eq!(
        fundamentals_schema().metadata().get(SCHEMA_METADATA_KEY),
        Some(&"reference.fundamentals".to_string())
    );
}

#[test]
fn classification_schema_pins_the_field_contracts() {
    let schema = classification_schema();
    assert_eq!(
        column_names(&schema),
        [
            ("file_creation_time", false),
            ("symbol", false),
            ("security_name", false),
            ("listing_exchange", false),
            ("market_category", false),
            ("etf", false),
            ("round_lot_size", false),
            ("test_issue", false),
            ("financial_status", true),
        ]
    );
    assert_eq!(
        schema
            .field_with_name("file_creation_time")
            .expect("field")
            .data_type(),
        &DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()))
    );
    assert_eq!(
        schema
            .field_with_name("round_lot_size")
            .expect("field")
            .data_type(),
        &DataType::Decimal128(38, 18)
    );
}

#[test]
fn fundamentals_schema_pins_the_field_contracts() {
    let schema = fundamentals_schema();
    assert_eq!(
        column_names(&schema),
        [
            ("cik", false),
            ("taxonomy", false),
            ("tag", false),
            ("unit", false),
            ("period_end", false),
            ("fy", true),
            ("fp", true),
            ("form", false),
            ("accn", false),
            ("filed", false),
            ("frame", true),
            ("val", false),
        ]
    );
    assert_eq!(
        schema
            .field_with_name("period_end")
            .expect("field")
            .data_type(),
        &DataType::Date32
    );
    assert_eq!(
        schema.field_with_name("cik").expect("field").data_type(),
        &DataType::UInt64
    );
    assert_eq!(
        schema.field_with_name("val").expect("field").data_type(),
        &DataType::Decimal128(38, 18)
    );
}

#[test]
fn classification_table_sorts_deterministically_by_symbol() {
    let table = classification_table(classification_rows()).expect("table");

    let schema = table.schema();
    assert_eq!(
        column_names(&schema),
        column_names(&classification_schema())
    );
    let symbol: Vec<&str> = arrow_downcast::<StringArray>(&table, "symbol")
        .iter()
        .flatten()
        .collect();
    assert_eq!(symbol, ["AAPL", "MSFT"]);
    let financial_status: Vec<Option<&str>> =
        arrow_downcast::<StringArray>(&table, "financial_status")
            .iter()
            .collect();
    assert_eq!(financial_status, [None, Some("Q")]);
    let round_lot: Vec<i128> = arrow_downcast::<Decimal128Array>(&table, "round_lot_size")
        .values()
        .to_vec();
    assert_eq!(
        round_lot,
        vec![
            Decimal18::parse("100").expect("lot").unscaled(),
            Decimal18::parse("100").expect("lot").unscaled(),
        ]
    );
}

#[test]
fn fundamentals_table_sorts_deterministically_by_identity() {
    let table = fundamentals_table(fundamentals_rows()).expect("table");

    let taxonomy: Vec<&str> = arrow_downcast::<StringArray>(&table, "taxonomy")
        .iter()
        .flatten()
        .collect();
    assert_eq!(taxonomy, ["dei", "us-gaap"]);
    let period_end: Vec<i32> = arrow_downcast::<Date32Array>(&table, "period_end")
        .values()
        .to_vec();
    assert_eq!(
        period_end,
        [
            Date::from_ymd(2023, 10, 20).expect("date").days(),
            Date::from_ymd(2023, 12, 30).expect("date").days(),
        ]
    );
    let fy: Vec<Option<i32>> = arrow_downcast::<Int32Array>(&table, "fy").iter().collect();
    assert_eq!(fy, [None, Some(2024)]);
    let filed: Vec<i32> = arrow_downcast::<Date32Array>(&table, "filed")
        .values()
        .to_vec();
    assert_eq!(
        filed,
        [
            Date::from_ymd(2023, 11, 3).expect("date").days(),
            Date::from_ymd(2024, 2, 2).expect("date").days(),
        ]
    );
}

#[test]
fn fundamentals_table_round_trips_through_the_reader() {
    let table = fundamentals_table(fundamentals_rows()).expect("table");
    let staged = crate::ingest::stage(&table, "test/fundamentals.parquet").expect("staged");

    let rows =
        read_fundamentals_rows(&staged.payload, "test/fundamentals.parquet", "test").expect("rows");
    let mut expected = fundamentals_rows();
    expected.sort();
    assert_eq!(rows, expected);
}

#[test]
fn classification_table_round_trips_through_the_reader() {
    let table = classification_table(classification_rows()).expect("table");
    let staged = crate::ingest::stage(&table, "test/snapshot.parquet").expect("staged");

    let rows =
        read_classification_rows(&staged.payload, "test/snapshot.parquet", "test").expect("rows");
    let mut expected = classification_rows();
    expected.sort_by(|left, right| left.symbol.cmp(&right.symbol));
    assert_eq!(rows, expected);
}

#[test]
fn objects_with_unexpected_schemas_are_rejected() {
    let foreign_schema = Schema::new(vec![Field::new("x", DataType::Int64, false)]);
    let batch = RecordBatch::new_empty(Arc::new(foreign_schema));
    let staged = crate::ingest::stage(&batch, "foreign.parquet").expect("staged");

    let error = read_fundamentals_rows(&staged.payload, "foreign.parquet", "reference")
        .expect_err("rejected");
    assert!(error.to_string().contains("unexpected schema"));
}

#[test]
fn fact_rows_sort_with_nulls_after_values() {
    let present = fundamentals_rows()[0].clone();
    let mut absent = present.clone();
    absent.fy = None;
    assert!(present < absent);

    let mut absent_fp = present.clone();
    absent_fp.fp = None;
    assert!(present < absent_fp);
}

#[test]
fn nasdaq_rows_convert_to_classification_rows() {
    let payload = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join("nasdaqtraded.txt");
    let snapshot = crate::nasdaq::parse_nasdaq_traded(&std::fs::read(payload).expect("fixture"))
        .expect("snapshot");
    let rows: Vec<ClassificationRow> = snapshot
        .securities
        .iter()
        .map(|security: &NasdaqSecurity| ClassificationRow {
            file_creation_time: snapshot.file_creation_time.utc(),
            symbol: security.symbol.clone(),
            security_name: security.security_name.clone(),
            listing_exchange: security.listing_exchange.clone(),
            market_category: security.market_category.clone(),
            etf: security.etf,
            round_lot_size: security.round_lot_size,
            test_issue: security.test_issue,
            financial_status: security.financial_status.clone(),
        })
        .collect();
    let table = classification_table(rows).expect("table");
    assert_eq!(table.num_rows(), 4);
}

#[test]
fn classification_rows_without_extras_stage_byte_identically() {
    let pinned = classification_table(classification_rows()).expect("table");
    let delegated = classification_table_with_extras(
        classification_rows()
            .into_iter()
            .map(|row| (row, ClassificationExtras::default()))
            .collect(),
    )
    .expect("table");

    assert_eq!(delegated.schema(), pinned.schema());
    let pinned = crate::ingest::stage(&pinned, "test/snapshot.parquet").expect("staged");
    let delegated = crate::ingest::stage(&delegated, "test/snapshot.parquet").expect("staged");
    assert_eq!(delegated.payload, pinned.payload);
    assert_eq!(delegated.metadata, pinned.metadata);
}

#[test]
fn classification_table_with_extras_appends_cik_after_the_pinned_fields() {
    let rows: Vec<(ClassificationRow, ClassificationExtras)> = classification_rows()
        .into_iter()
        .map(|row| {
            let cik = (row.symbol == "AAPL").then_some(320193);
            (
                row,
                ClassificationExtras {
                    cik,
                    ..ClassificationExtras::default()
                },
            )
        })
        .collect();
    let table = classification_table_with_extras(rows).expect("table");

    let schema = table.schema();
    let pinned = classification_schema();
    let mut expected = column_names(&pinned);
    expected.push(("cik", true));
    assert_eq!(column_names(&schema), expected);
    assert_eq!(
        schema.metadata().get(SCHEMA_METADATA_KEY),
        Some(&"reference.classification".to_string())
    );
    assert_eq!(
        schema.metadata().get(SCHEMA_VERSION_METADATA_KEY),
        Some(&REFERENCE_SCHEMA_VERSION.to_string())
    );
    let cik: Vec<Option<u64>> = arrow_downcast::<UInt64Array>(&table, "cik")
        .iter()
        .collect();
    assert_eq!(cik, [Some(320193), None]);

    let staged = crate::ingest::stage(&table, "test/snapshot.parquet").expect("staged");
    let rows =
        read_classification_rows_with_extras(&staged.payload, "test/snapshot.parquet", "test")
            .expect("rows");
    assert_eq!(rows[0].0.symbol, "AAPL");
    assert_eq!(rows[0].1.cik, Some(320193));
    assert_eq!(rows[1].0.symbol, "MSFT");
    assert_eq!(rows[1].1.cik, None);
    assert_eq!(rows[1].1, ClassificationExtras::default());
}

#[test]
fn classification_table_with_extras_writes_sic_columns_when_carried() {
    let rows: Vec<(ClassificationRow, ClassificationExtras)> = classification_rows()
        .into_iter()
        .map(|row| {
            let extras = if row.symbol == "AAPL" {
                ClassificationExtras {
                    cik: Some(320193),
                    sic: Some("3571".to_string()),
                    sic_industry: Some("ELECTRONIC COMPUTERS".to_string()),
                }
            } else {
                ClassificationExtras::default()
            };
            (row, extras)
        })
        .collect();
    let table = classification_table_with_extras(rows).expect("table");

    let pinned = classification_schema();
    let mut expected = column_names(&pinned);
    expected.extend([("cik", true), ("sic", true), ("sic_industry", true)]);
    assert_eq!(column_names(&table.schema()), expected);

    let staged = crate::ingest::stage(&table, "test/snapshot.parquet").expect("staged");
    let rows =
        read_classification_rows_with_extras(&staged.payload, "test/snapshot.parquet", "test")
            .expect("rows");
    assert_eq!(
        rows[0].1,
        ClassificationExtras {
            cik: Some(320193),
            sic: Some("3571".to_string()),
            sic_industry: Some("ELECTRONIC COMPUTERS".to_string()),
        }
    );
    assert_eq!(rows[1].1, ClassificationExtras::default());
}

#[test]
fn pinned_snapshots_read_with_absent_extras() {
    let table = classification_table(classification_rows()).expect("table");
    let staged = crate::ingest::stage(&table, "test/snapshot.parquet").expect("staged");

    let rows =
        read_classification_rows_with_extras(&staged.payload, "test/snapshot.parquet", "test")
            .expect("rows");
    assert_eq!(rows.len(), 2);
    assert!(rows
        .iter()
        .all(|(_, extras)| *extras == ClassificationExtras::default()));
}

#[test]
fn mistyped_or_unknown_extras_are_rejected() {
    let cases = [
        Field::new("cik", DataType::Utf8, true),
        Field::new("sic", DataType::Int64, true),
        Field::new("mystery", DataType::Utf8, true),
    ];

    for extra in cases {
        let base = classification_schema();
        let mut fields: Vec<FieldRef> = base.fields().iter().cloned().collect();
        fields.push(Arc::new(extra));
        let schema = Schema::new_with_metadata(fields, base.metadata().clone());
        let batch = RecordBatch::new_empty(Arc::new(schema));
        let staged = crate::ingest::stage(&batch, "test/snapshot.parquet").expect("staged");

        let error =
            read_classification_rows_with_extras(&staged.payload, "test/snapshot.parquet", "test")
                .expect_err("rejected");
        assert!(error.to_string().contains("unexpected schema"), "{error}");
    }
}

fn arrow_downcast<'a, A: arrow_array::Array + 'static>(
    table: &'a RecordBatch,
    name: &str,
) -> &'a A {
    table
        .column_by_name(name)
        .expect("column")
        .as_any()
        .downcast_ref::<A>()
        .expect("array type")
}
