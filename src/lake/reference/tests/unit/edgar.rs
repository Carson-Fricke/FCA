use std::path::Path;

use lake_contracts::{Date, Decimal18};
use serde_json::{json, Value};

use super::*;

fn fixtures_dir() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/lake/reference/fixtures")
}

fn company_facts_bytes() -> Vec<u8> {
    std::fs::read(fixtures_dir().join("companyfacts.json")).expect("fixture")
}

fn company_facts_document() -> Value {
    serde_json::from_slice(&company_facts_bytes()).expect("fixture json")
}

fn date(text: &str) -> Date {
    Date::parse(text).expect("valid date")
}

#[test]
fn companyfacts_document_expands_every_taxonomy_tag_and_unit() {
    let facts = parse_companyfacts_bytes(&company_facts_bytes()).expect("facts");

    assert_eq!(facts.len(), 4);
    let taxonomies: std::collections::HashSet<&str> =
        facts.iter().map(|fact| fact.taxonomy.as_str()).collect();
    assert_eq!(taxonomies, ["dei", "us-gaap"].into_iter().collect());
    let ciks: std::collections::HashSet<u64> = facts.iter().map(|fact| fact.cik).collect();
    assert_eq!(ciks, [320193].into());
}

#[test]
fn companyfacts_rows_carry_exact_identity_and_values() {
    let facts = parse_companyfacts(&company_facts_document()).expect("facts");

    let shares = facts
        .iter()
        .find(|fact| fact.taxonomy == "dei")
        .expect("dei fact");
    assert_eq!(shares.tag, "EntityCommonStockSharesOutstanding");
    assert_eq!(shares.unit, "shares");
    assert_eq!(shares.period_end, date("2023-10-20"));
    assert_eq!(shares.fy, Some(2023));
    assert_eq!(shares.fp.as_deref(), Some("FY"));
    assert_eq!(shares.form, "10-K");
    assert_eq!(shares.accn, "0000320193-23-000106");
    assert_eq!(shares.filed, date("2023-11-03"));
    assert_eq!(shares.frame.as_deref(), Some("CY2023Q3I"));
    assert_eq!(shares.val, Decimal18::parse("15464661000").expect("val"));

    let revenues: Vec<&XbrlFact> = facts.iter().filter(|fact| fact.tag == "Revenues").collect();
    assert_eq!(
        revenues.iter().map(|fact| fact.filed).collect::<Vec<_>>(),
        [date("2024-02-02"), date("2024-05-03")]
    );
    assert_eq!(
        revenues.iter().map(|fact| fact.val).collect::<Vec<_>>(),
        [
            Decimal18::parse("119575000000").expect("val"),
            Decimal18::parse("119999000000").expect("val"),
        ]
    );
    assert_eq!(
        revenues
            .iter()
            .map(|fact| fact.frame.clone())
            .collect::<Vec<_>>(),
        [Some("CY2023Q4".to_string()), None]
    );
}

fn revenues_of(facts: &[XbrlFact]) -> Vec<&XbrlFact> {
    facts.iter().filter(|fact| fact.tag == "Revenues").collect()
}

#[test]
fn restatement_rows_share_the_tag_and_period_but_not_the_filing() {
    let facts = parse_companyfacts(&company_facts_document()).expect("facts");
    let revenues = revenues_of(&facts);
    assert_eq!(revenues.len(), 2);

    let (original, restatement) = (revenues[0], revenues[1]);
    assert_eq!(original.period_end, restatement.period_end);
    assert!(original.filed < restatement.filed);
    assert_ne!(original.accn, restatement.accn);
    assert_ne!(original.val, restatement.val);
}

#[test]
fn bytes_and_json_documents_parse_identically() {
    let from_bytes = parse_companyfacts_bytes(&company_facts_bytes()).expect("facts");
    let from_document = parse_companyfacts(&company_facts_document()).expect("facts");
    assert_eq!(from_bytes, from_document);
}

#[test]
fn duplicate_rows_collapse_while_archive_order_is_preserved() {
    let facts = parse_companyfacts(&company_facts_document()).expect("facts");

    let duplicated = deduplicate_facts(&[facts.clone(), facts.clone()].concat());

    assert_eq!(duplicated, facts);
}

#[test]
fn companyconcept_document_expands_to_its_single_taxonomy_and_tag() {
    let document = json!({
        "cik": 320193,
        "taxonomy": "us-gaap",
        "tag": "Revenues",
        "label": "Revenue",
        "units": {
            "USD": [
                {
                    "end": "2023-12-30",
                    "val": "119575000000",
                    "accn": "0000320193-24-000006",
                    "fy": 2024,
                    "fp": "Q1",
                    "form": "10-Q",
                    "filed": "2024-02-02",
                }
            ]
        },
    });

    let facts = parse_companyconcept(&document).expect("facts");

    assert_eq!(facts.len(), 1);
    let fact = &facts[0];
    assert_eq!(
        (
            fact.cik,
            fact.taxonomy.as_str(),
            fact.tag.as_str(),
            fact.unit.as_str()
        ),
        (320193, "us-gaap", "Revenues", "USD")
    );
    assert_eq!(fact.val, Decimal18::parse("119575000000").expect("val"));
    assert_eq!(fact.frame, None);
}

#[test]
fn url_templates_format_the_pinned_sec_shapes() {
    let filings = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json";
    assert_eq!(
        company_facts_url(filings, 320193).expect("url"),
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    );
    assert_eq!(
        company_concept_url(
            "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}/{taxonomy}/{tag}.json",
            320193,
            "us-gaap",
            "Revenues",
        )
        .expect("url"),
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/Revenues.json"
    );
}

#[test]
fn invalid_url_templates_are_rejected() {
    for template in ["https://example.test/{unknown}", "https://example.test/{}"] {
        let error = company_facts_url(template, 320193).expect_err("rejected");
        assert!(error.to_string().contains("invalid SEC facts URL template"));
    }
}

#[test]
fn missing_or_invalid_cik_is_rejected() {
    for document in [
        json!({}),
        json!({"cik": "320193"}),
        json!({"cik": 0}),
        json!({"cik": true}),
        json!({"cik": -1}),
    ] {
        let error = parse_companyfacts(&document).expect_err("rejected");
        assert!(error.to_string().contains("cik"));
    }
}

#[test]
fn document_with_missing_facts_or_bad_shape_is_rejected() {
    let cik = json!({"cik": 320193});
    let error = parse_companyfacts(&cik).expect_err("rejected");
    assert!(error.to_string().contains("missing the \"facts\" object"));

    let error = parse_companyfacts(&json!({"cik": 320193, "facts": {"us-gaap": "nope"}}))
        .expect_err("rejected");
    assert!(error.to_string().contains("must be an object"));

    let error = parse_companyfacts(&json!({"cik": 320193, "facts": {"us-gaap": {"Assets": {}}}}))
        .expect_err("rejected");
    assert!(error.to_string().contains("missing its units object"));

    let error = parse_companyfacts(&json!({
        "cik": 320193,
        "facts": {"us-gaap": {"Assets": {"units": {"USD": {}}}}},
    }))
    .expect_err("rejected");
    assert!(error.to_string().contains("must be an array"));

    let error = parse_companyfacts(&json!({
        "cik": 320193,
        "facts": {"us-gaap": {"Assets": {"units": {"USD": ["nope"]}}}},
    }))
    .expect_err("rejected");
    assert!(error.to_string().contains("must be an object"));

    let error = parse_companyfacts_bytes(b"[1, 2, 3]").expect_err("rejected");
    assert!(error.to_string().contains("root must be an object"));
}

#[test]
fn entry_field_validation_is_rejected_with_context() {
    let entry = json!({
        "end": "2023-12-30",
        "val": 1,
        "accn": "0000320193-24-000006",
        "form": "10-Q",
        "filed": "2024-02-02",
    });
    let document = |unit_entry: Value| {
        json!({
            "cik": 320193,
            "taxonomy": "us-gaap",
            "tag": "Assets",
            "units": {"USD": [unit_entry]},
        })
    };

    let mut without_end = entry.clone();
    without_end.as_object_mut().expect("object").remove("end");
    assert!(parse_companyconcept(&document(without_end))
        .expect_err("rejected")
        .to_string()
        .contains("is missing \"end\""));

    let mut without_form = entry.clone();
    without_form.as_object_mut().expect("object").remove("form");
    assert!(parse_companyconcept(&document(without_form))
        .expect_err("rejected")
        .to_string()
        .contains("is missing \"form\""));

    let mut without_accn = entry.clone();
    without_accn.as_object_mut().expect("object").remove("accn");
    assert!(parse_companyconcept(&document(without_accn))
        .expect_err("rejected")
        .to_string()
        .contains("is missing \"accn\""));

    let mut bad_filed = entry.clone();
    bad_filed.as_object_mut().expect("object")["filed"] = json!("not-a-date");
    assert!(parse_companyconcept(&document(bad_filed))
        .expect_err("rejected")
        .to_string()
        .contains("invalid \"filed\""));

    let mut bad_val = entry.clone();
    bad_val.as_object_mut().expect("object")["val"] = json!("many");
    assert!(parse_companyconcept(&document(bad_val))
        .expect_err("rejected")
        .to_string()
        .contains("non-numeric 'val'"));

    let mut nan_val = entry.clone();
    nan_val.as_object_mut().expect("object")["val"] = json!("NaN");
    assert!(parse_companyconcept(&document(nan_val))
        .expect_err("rejected")
        .to_string()
        .contains("non-finite 'val'"));

    let mut bad_fy = entry.clone();
    bad_fy
        .as_object_mut()
        .expect("object")
        .insert("fy".to_string(), json!("2024"));
    assert!(parse_companyconcept(&document(bad_fy))
        .expect_err("rejected")
        .to_string()
        .contains("non-integer \"fy\""));
}

#[test]
fn float_values_keep_exact_decimal_text() {
    let mut entry = json!({
        "end": "2023-12-30",
        "val": 100,
        "accn": "0000320193-24-000006",
        "fy": 2024,
        "fp": "Q1",
        "form": "10-Q",
        "filed": "2024-02-02",
    });
    entry.as_object_mut().expect("object")["val"] =
        serde_json::json!(serde_json::Number::from_f64(0.1).expect("number"));
    let facts = parse_companyconcept(&json!({
        "cik": 320193,
        "taxonomy": "us-gaap",
        "tag": "WeightedAverageNumberOfSharesOutstandingBasic",
        "units": {"shares": [entry]},
    }))
    .expect("facts");

    assert_eq!(facts[0].val, Decimal18::parse("0.1").expect("val"));
}

#[test]
fn boolean_and_object_values_are_rejected_for_val() {
    let entry = json!({
        "end": "2023-12-30",
        "val": true,
        "accn": "0000320193-24-000006",
        "form": "10-Q",
        "filed": "2024-02-02",
    });
    let error = parse_companyconcept(&json!({
        "cik": 320193,
        "taxonomy": "us-gaap",
        "tag": "Assets",
        "units": {"USD": [entry]},
    }))
    .expect_err("rejected");
    assert!(error.to_string().contains("non-numeric 'val'"));
}

#[test]
fn format_templates_handle_spec_shapes_and_escapes() {
    let rendered = format_template(
        "{{CIK}}{cik:0>10}{{END}}",
        &[("cik", "320193".to_string())],
        "invalid",
    )
    .expect("rendered");
    assert_eq!(rendered, "{CIK}0000320193{END}");

    assert!(format_template(
        "https://x.test/{cik:10}",
        &[("cik", "320193".to_string())],
        "invalid"
    )
    .is_ok());
    assert!(format_template(
        "https://x.test/{cik}",
        &[("cik", "320193".to_string())],
        "invalid"
    )
    .is_ok());
    assert!(format_template(
        "https://x.test/{unknown}",
        &[("cik", "320193".to_string())],
        "invalid"
    )
    .is_err());
    assert!(format_template(
        "https://x.test/{}",
        &[("cik", "320193".to_string())],
        "invalid"
    )
    .is_err());
    assert!(format_template(
        "https://x.test/{cik",
        &[("cik", "320193".to_string())],
        "invalid"
    )
    .is_err());
}

#[test]
fn concept_document_requires_text_fields() {
    let document = json!({
        "cik": 320193,
        "taxonomy": "",
        "tag": "Revenues",
        "units": {},
    });
    let error = parse_companyconcept(&document).expect_err("rejected");
    assert!(error.to_string().contains("missing \"taxonomy\""));
}
