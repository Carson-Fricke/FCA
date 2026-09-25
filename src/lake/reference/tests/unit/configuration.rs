use std::collections::BTreeMap;
use std::path::Path;

use super::*;

fn environ(entries: &[(&str, &str)]) -> BTreeMap<String, String> {
    entries
        .iter()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect()
}

fn defaults_value() -> serde_json::Value {
    serde_json::json!({
        "schema_version": 1,
        "user_agent": "FCA market lake ingest AdminContact@example.com",
        "classification_url": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt",
        "company_facts_url_template":
            "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json",
        "company_concept_url_template":
            "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}/{taxonomy}/{tag}.json",
        "bulk_company_facts_url":
            "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip",
        "http": {
            "requests_per_second": 10,
            "timeout_seconds": 30,
            "retry": {
                "max_attempts": 5,
                "honor_retry_after": true,
                "backoff_seconds": 0.5,
                "max_backoff_seconds": 30.0,
            },
        },
    })
}

#[test]
fn committed_reference_yaml_loads_with_sec_fair_access_defaults() {
    let settings = load_reference_settings(None, &environ(&[])).expect("settings");

    assert_eq!(settings.schema_version, 1);
    assert!((settings.http.requests_per_second - 10.0).abs() < f64::EPSILON);
    assert_eq!(settings.http.timeout_seconds, 30);
    assert_eq!(settings.http.retry.max_attempts, 5);
    assert!(settings.http.retry.honor_retry_after);
    assert!(settings.classification_url.ends_with("nasdaqtraded.txt"));
    assert!(settings
        .company_facts_url_template
        .ends_with("CIK{cik:0>10}.json"));
    assert!(settings.company_concept_url_template.contains("{taxonomy}"));
    assert!(settings.company_concept_url_template.contains("{tag}"));
    assert!(settings
        .bulk_company_facts_url
        .ends_with("companyfacts.zip"));
    assert!(settings.user_agent.contains('@'));
}

#[test]
fn environment_overrides_win_without_process_globals() {
    let settings = load_reference_settings(
        None,
        &environ(&[
            (
                "LAKE_REFERENCE__USER_AGENT",
                "Other Company contact@other.example",
            ),
            ("LAKE_REFERENCE__HTTP__RETRY__MAX_ATTEMPTS", "2"),
        ]),
    )
    .expect("settings");

    assert_eq!(settings.user_agent, "Other Company contact@other.example");
    assert_eq!(settings.http.retry.max_attempts, 2);
    assert!((settings.http.requests_per_second - 10.0).abs() < f64::EPSILON);
}

#[test]
fn invalid_user_agent_is_rejected() {
    let mut defaults = defaults_value();
    defaults["user_agent"] = serde_json::json!("anonymous");
    let error = ReferenceSettings::from_value(&defaults).expect_err("rejected");
    assert!(error.message.contains("user_agent"));

    defaults["user_agent"] = serde_json::json!("   ");
    assert!(ReferenceSettings::from_value(&defaults).is_err());
}

#[test]
fn rate_cap_above_the_sec_fair_access_limit_is_rejected() {
    let mut defaults = defaults_value();
    defaults["http"]["requests_per_second"] = serde_json::json!(11);
    let error = ReferenceSettings::from_value(&defaults).expect_err("rejected");
    assert!(error
        .message
        .contains("requests_per_second must be greater than 0 and at most 10"));

    defaults["http"]["requests_per_second"] = serde_json::json!(0);
    assert!(ReferenceSettings::from_value(&defaults).is_err());
}

#[test]
fn templates_missing_placeholders_are_rejected() {
    let mut defaults = defaults_value();
    defaults["company_facts_url_template"] = serde_json::json!("https://data.sec.gov/facts.json");
    let error = ReferenceSettings::from_value(&defaults).expect_err("rejected");
    assert!(error.message.contains("placeholder"));

    let mut defaults = defaults_value();
    defaults["company_concept_url_template"] =
        serde_json::json!("https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}.json");
    assert!(ReferenceSettings::from_value(&defaults).is_err());

    let mut defaults = defaults_value();
    defaults["company_concept_url_template"] =
        serde_json::json!("https://data.sec.gov/api/xbrl/companyconcept/{cik}/{taxonomy}/{tag}/{}");
    let error = ReferenceSettings::from_value(&defaults).expect_err("rejected");
    assert!(error.message.contains("invalid URL template"));
}

#[test]
fn unknown_fields_are_rejected() {
    let mut defaults = defaults_value();
    defaults["unknown"] = serde_json::json!(1);
    assert!(ReferenceSettings::from_value(&defaults).is_err());

    let mut defaults = defaults_value();
    defaults["http"]["unknown"] = serde_json::json!(1);
    assert!(ReferenceSettings::from_value(&defaults).is_err());

    let mut defaults = defaults_value();
    defaults["http"]["retry"]["unknown"] = serde_json::json!(1);
    assert!(ReferenceSettings::from_value(&defaults).is_err());
}

#[test]
fn out_of_bounds_http_values_are_rejected() {
    let mut defaults = defaults_value();
    defaults["http"]["timeout_seconds"] = serde_json::json!(0);
    assert!(ReferenceSettings::from_value(&defaults).is_err());

    let mut defaults = defaults_value();
    defaults["http"]["retry"]["max_attempts"] = serde_json::json!(0);
    assert!(ReferenceSettings::from_value(&defaults).is_err());

    let mut defaults = defaults_value();
    defaults["http"]["retry"]["max_backoff_seconds"] = serde_json::json!(0);
    assert!(ReferenceSettings::from_value(&defaults).is_err());
}

#[test]
fn config_dir_resolution_is_repository_relative() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf();
    assert_eq!(
        reference_config_dir(Some(&root)),
        root.join("config").join("lake")
    );
    assert_eq!(
        reference_config_dir(Some(&root.join("config").join("lake"))),
        root.join("config").join("lake")
    );
}

#[test]
fn load_reference_settings_applies_the_committed_yaml() {
    let settings = load_reference_settings(None, &environ(&[])).expect("settings");
    assert_eq!(
        settings,
        ReferenceSettings::from_value(&defaults_value()).expect("defaults")
    );
}
