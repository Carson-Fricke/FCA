use super::*;

fn field_types(schema: &Schema) -> Vec<(String, String, bool)> {
    schema
        .fields()
        .iter()
        .map(|field| {
            (
                field.name().clone(),
                format!("{}", field.data_type()),
                field.is_nullable(),
            )
        })
        .collect()
}

#[test]
fn equity_trade_schema_pins_fields_types_and_metadata() {
    let schema = schema_for(AssetClass::Equity, DataType::Trade).expect("schema");
    assert_eq!(
        schema
            .metadata()
            .get(SCHEMA_METADATA_KEY)
            .map(String::as_str),
        Some("equity.trade")
    );
    assert_eq!(
        schema
            .metadata()
            .get(SCHEMA_VERSION_METADATA_KEY)
            .map(String::as_str),
        Some("1")
    );
    assert_eq!(
        field_types(&schema),
        vec![
            ("timestamp".into(), "Timestamp(ns, \"UTC\")".into(), false),
            ("symbol".into(), "Utf8".into(), false),
            ("trade_id".into(), "UInt64".into(), false),
            ("exchange".into(), "Utf8".into(), false),
            ("price".into(), "Decimal128(38, 18)".into(), false),
            ("size".into(), "UInt32".into(), false),
            ("conditions".into(), "List(non-null Utf8)".into(), false),
            ("tape".into(), "Utf8".into(), false),
            ("update".into(), "Utf8".into(), true),
        ]
    );
}

#[test]
fn crypto_schemas_have_nullable_provider_optional_columns() {
    let trade = schema_for(AssetClass::Crypto, DataType::Trade).expect("crypto trade");
    let nullable: Vec<&str> = trade
        .fields()
        .iter()
        .filter(|field| field.is_nullable())
        .map(|field| field.name().as_str())
        .collect();
    assert_eq!(nullable, vec!["trade_id", "exchange", "taker_side"]);
    let quote = schema_for(AssetClass::Crypto, DataType::Quote).expect("crypto quote");
    let nullable: Vec<&str> = quote
        .fields()
        .iter()
        .filter(|field| field.is_nullable())
        .map(|field| field.name().as_str())
        .collect();
    assert_eq!(nullable, vec!["bid_exchange", "ask_exchange"]);
}

#[test]
fn unsupported_datasets_have_no_schema() {
    assert!(schema_for(AssetClass::Reference, DataType::Classification).is_err());
    assert_eq!(
        schema_name(AssetClass::Option, DataType::Quote),
        Some("option.quote")
    );
    assert_eq!(schema_name(AssetClass::Reference, DataType::Trade), None);
}

#[test]
fn equity_bars_schema_pins_fields_types_and_metadata() {
    let schema = schema_for(AssetClass::Equity, DataType::Bars).expect("schema");
    assert_eq!(
        schema
            .metadata()
            .get(SCHEMA_METADATA_KEY)
            .map(String::as_str),
        Some("equity.bars")
    );
    assert_eq!(
        schema
            .metadata()
            .get(SCHEMA_VERSION_METADATA_KEY)
            .map(String::as_str),
        Some("1")
    );
    assert_eq!(
        field_types(&schema),
        vec![
            ("timestamp".into(), "Timestamp(ns, \"UTC\")".into(), false),
            ("symbol".into(), "Utf8".into(), false),
            ("open".into(), "Decimal128(38, 18)".into(), false),
            ("high".into(), "Decimal128(38, 18)".into(), false),
            ("low".into(), "Decimal128(38, 18)".into(), false),
            ("close".into(), "Decimal128(38, 18)".into(), false),
            ("volume".into(), "Decimal128(38, 18)".into(), false),
        ]
    );
    assert_eq!(
        schema_name(AssetClass::Equity, DataType::Bars),
        Some("equity.bars")
    );
}

#[test]
fn crypto_bars_schema_mirrors_equity_bars_metadata() {
    let schema = schema_for(AssetClass::Crypto, DataType::Bars).expect("schema");
    assert_eq!(
        schema
            .metadata()
            .get(SCHEMA_METADATA_KEY)
            .map(String::as_str),
        Some("crypto.bars")
    );
    assert_eq!(
        schema
            .metadata()
            .get(SCHEMA_VERSION_METADATA_KEY)
            .map(String::as_str),
        Some("1")
    );
    assert_eq!(schema.fields().len(), 7);
    let expected = schema_for(AssetClass::Equity, DataType::Bars).expect("equity bars");
    assert_eq!(field_types(&schema), field_types(&expected));
    assert_eq!(
        schema_name(AssetClass::Crypto, DataType::Bars),
        Some("crypto.bars")
    );
    assert!(schema_name(AssetClass::Option, DataType::Bars).is_none());
    assert!(schema_for(AssetClass::Option, DataType::Bars).is_err());
}
