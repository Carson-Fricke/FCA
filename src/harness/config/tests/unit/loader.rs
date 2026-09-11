use super::*;

#[test]
fn resolves_references_and_names() {
    assert_eq!(
        environment_variable_name("${HARNESS_API_BEARER_TOKEN}").expect("name"),
        "HARNESS_API_BEARER_TOKEN"
    );
    assert!(environment_variable_name("HARNESS_API_BEARER_TOKEN").is_err());
    let mut environ = BTreeMap::new();
    environ.insert("A".to_string(), "value".to_string());
    assert_eq!(
        resolve_environment_reference("${A}", Some(&environ)).expect("resolve"),
        "value"
    );
    assert!(resolve_environment_reference("${B}", Some(&environ)).is_err());
}

#[test]
fn rejects_committed_secret_values() {
    let text = "shell: api\npassword: hunter2\n";
    let value = crate::yaml::parse_yaml_mapping(text)
        .map(YamlValue::Map)
        .expect("yaml");
    assert!(reject_committed_secrets(&value, "").is_err());
    let safe = "shell: api\npassword: \"${PASSWORD}\"\n";
    let value = crate::yaml::parse_yaml_mapping(safe)
        .map(YamlValue::Map)
        .expect("yaml");
    assert!(reject_committed_secrets(&value, "").is_ok());
}
