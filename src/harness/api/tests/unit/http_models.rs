use super::*;

#[test]
fn error_envelope_omits_null_details() {
    let value = error_body("run_not_found", "missing", None);
    assert_eq!(
        value,
        json!({"error": {"code": "run_not_found", "message": "missing"}})
    );
}

#[test]
fn error_envelope_includes_details_when_present() {
    let value = error_body("validation_error", "bad", Some(json!({"field": "x"})));
    assert_eq!(
        value,
        json!({"error": {"code": "validation_error", "message": "bad", "details": {"field": "x"}}})
    );
}
