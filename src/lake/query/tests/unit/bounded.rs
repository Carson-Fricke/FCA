//! Unit tests for the private bounded-query helpers.

use super::{decimal_text, tokenize_sql, validate_read_only_sql, BoundedQueryError};

#[test]
fn decimal128_text_renders_like_python_str_decimal() {
    assert_eq!(
        decimal_text(190_125_000_000_000_000_000, 18),
        "190.125000000000000000"
    );
    assert_eq!(
        decimal_text(-190_125_000_000_000_000_000, 18),
        "-190.125000000000000000"
    );
    assert_eq!(
        decimal_text(-500_000_000_000_000_000, 18),
        "-0.500000000000000000"
    );
    assert_eq!(decimal_text(0, 18), "0E-18");
    assert_eq!(decimal_text(1, 18), "1E-18");
    assert_eq!(decimal_text(5, 18), "5E-18");
    assert_eq!(decimal_text(5, 0), "5");
    assert_eq!(decimal_text(1, 6), "0.000001");
    assert_eq!(decimal_text(1, 7), "1E-7");
    assert_eq!(
        decimal_text(12_345_678_901_234_567_890_123_456_789_012_345_678, 18),
        "12345678901234567890.123456789012345678"
    );
    assert_eq!(decimal_text(0, 0), "0");
    assert_eq!(decimal_text(123, 0), "123");
}

#[test]
fn tokenizer_splits_identifiers_strings_and_symbols() {
    let tokens = tokenize_sql("SELECT trade_id FROM data").expect("tokens");
    assert_eq!(tokens.len(), 4);
    assert_eq!(tokens[0].kind, super::TokenKind::Identifier);
    assert_eq!(tokens[0].value, "SELECT");
    assert_eq!(tokens[3].value, "DATA");

    let tokens = tokenize_sql("'C:/outside.parquet'").expect("string");
    assert_eq!(tokens.len(), 1);
    assert_eq!(tokens[0].kind, super::TokenKind::String);
    assert_eq!(tokens[0].value, "'C:/outside.parquet'");

    let tokens = tokenize_sql(r#""mixed"."data""#).expect("quoted");
    assert_eq!(tokens.len(), 3);
    assert_eq!(tokens[0].kind, super::TokenKind::QuotedIdentifier);
    assert_eq!(tokens[1].value, ".");

    let tokens = tokenize_sql("SELECT 'it''s' AS v").expect("escaped string");
    assert_eq!(tokens[1].value, "'it''s'");
    assert_eq!(tokens[2].value, "AS");
}

#[test]
fn tokenizer_rejects_unterminated_literals_and_comments() {
    assert_eq!(
        tokenize_sql("SELECT 'oops").expect_err("unterminated string"),
        BoundedQueryError::new("sql_not_allowed", "unterminated SQL string")
    );
    assert_eq!(
        tokenize_sql("SELECT \"oops").expect_err("unterminated identifier"),
        BoundedQueryError::new("sql_not_allowed", "unterminated SQL identifier")
    );
    assert_eq!(
        tokenize_sql("SELECT /* oops").expect_err("unterminated comment"),
        BoundedQueryError::new("sql_not_allowed", "unterminated SQL comment")
    );
}

#[test]
fn validator_accepts_selects_and_ctes_with_comments() {
    // The Python validator returns `sql.strip()`: whitespace is trimmed but
    // comments are preserved for the DuckDB statement.
    assert_eq!(
        validate_read_only_sql("  SELECT trade_id FROM data -- trailing\n").expect("valid"),
        "SELECT trade_id FROM data -- trailing"
    );
    assert_eq!(
        validate_read_only_sql("/* leading */ WITH t AS (SELECT 1 AS x FROM data) SELECT * FROM t")
            .expect("valid cte"),
        "/* leading */ WITH t AS (SELECT 1 AS x FROM data) SELECT * FROM t"
    );
    assert_eq!(
        validate_read_only_sql("SELECT * FROM LATERAL (SELECT 1) l").expect("lateral skipped"),
        "SELECT * FROM LATERAL (SELECT 1) l"
    );
    validate_read_only_sql("SELECT * FROM data WHERE price > '190'").expect("string value ok");
}

#[test]
fn validator_rejects_disallowed_statements() {
    let rejected = |sql: &str| validate_read_only_sql(sql).expect_err("rejected");
    assert_eq!(
        rejected(""),
        BoundedQueryError::new(
            "sql_not_allowed",
            "sql must be a non-empty read-only statement"
        )
    );
    assert_eq!(
        rejected("   "),
        BoundedQueryError::new(
            "sql_not_allowed",
            "sql must be a non-empty read-only statement"
        )
    );
    assert_eq!(
        rejected("DROP TABLE data"),
        BoundedQueryError::new("sql_not_allowed", "only SELECT statements are allowed")
    );
    assert_eq!(
        rejected("\"data\""),
        BoundedQueryError::new("sql_not_allowed", "only SELECT statements are allowed")
    );
    assert_eq!(
        rejected("SELECT 1; SELECT 2"),
        BoundedQueryError::new("sql_not_allowed", "multiple SQL statements are not allowed")
    );
    assert_eq!(
        rejected("SELECT * FROM data; INSTALL httpfs"),
        BoundedQueryError::new("sql_not_allowed", "multiple SQL statements are not allowed")
    );
    // The Python validator rejects a non-SELECT first token before the
    // dangerous-keyword scan, so UPDATE reports the SELECT rejection.
    assert_eq!(
        rejected("UPDATE data SET price = 1"),
        BoundedQueryError::new("sql_not_allowed", "only SELECT statements are allowed")
    );
    // The multiple-statement check precedes the dangerous-keyword scan.
    assert_eq!(
        rejected("SELECT * FROM data WHERE 1 = (SELECT 1); PRAGMA version"),
        BoundedQueryError::new("sql_not_allowed", "multiple SQL statements are not allowed")
    );
    assert_eq!(
        rejected("SET enable_external_access = true"),
        BoundedQueryError::new("sql_not_allowed", "only SELECT statements are allowed")
    );
}

#[test]
fn validator_rejects_storage_paths_and_external_functions() {
    let rejected = |sql: &str| validate_read_only_sql(sql).expect_err("rejected");
    assert_eq!(
        rejected("SELECT * FROM read_parquet('C:/outside.parquet')"),
        BoundedQueryError::new(
            "storage_path_not_allowed",
            "SQL cannot invoke external storage table functions"
        )
    );
    assert_eq!(
        rejected("SELECT parquet_schema('C:/outside.parquet') FROM data"),
        BoundedQueryError::new(
            "storage_path_not_allowed",
            "SQL cannot invoke external storage table functions"
        )
    );
    assert_eq!(
        rejected("SELECT * FROM '/absolute/outside.parquet'"),
        BoundedQueryError::new(
            "storage_path_not_allowed",
            "SQL cannot use a storage path as a relation"
        )
    );
    assert_eq!(
        rejected("SELECT * FROM 'C:/outside.parquet'"),
        BoundedQueryError::new(
            "storage_path_not_allowed",
            "SQL cannot use a storage path as a relation"
        )
    );
    assert_eq!(
        rejected("SELECT * FROM 'C:/outside.parquet'"),
        BoundedQueryError::new(
            "storage_path_not_allowed",
            "SQL cannot use a storage path as a relation"
        )
    );
    // A backtick-quoted path is a quoted identifier, not a string literal, so
    // the lexical guard lets it through; DuckDB then rejects the relation.
    validate_read_only_sql("SELECT * FROM `C:/outside.parquet`").expect("lexical guard passes");
    validate_read_only_sql("SELECT * FROM GLOB('*.parquet')").expect_err("glob rejected");
    validate_read_only_sql("SELECT HTTP_GET('http://example.test')").expect_err("http rejected");
}
