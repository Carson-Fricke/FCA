use lake_coverage::CoverageRequestError;
use serde_json::json;

use super::QueryApplicationError;

#[test]
fn display_returns_the_message() {
    let error = QueryApplicationError::new(
        "coverage_incomplete",
        "requested coverage is incomplete",
        None,
    );
    assert_eq!(error.to_string(), "requested coverage is incomplete");
    let dynamic: &dyn std::error::Error = &error;
    assert_eq!(dynamic.to_string(), "requested coverage is incomplete");
}

#[test]
fn new_keeps_the_python_error_shape() {
    let error = QueryApplicationError::new(
        "ensure_failed",
        "coverage ensure failed",
        Some(json!({"error": "launcher exploded"})),
    );
    assert_eq!(error.code, "ensure_failed");
    assert_eq!(error.message, "coverage ensure failed");
    assert_eq!(error.details, Some(json!({"error": "launcher exploded"})));
}

#[test]
fn coverage_rejections_keep_code_message_and_details() {
    let rejection = CoverageRequestError::new(
        "invalid_date_range",
        "start must be on or before end",
        Some(json!({"start": "2024-01-19"})),
    );
    let error = QueryApplicationError::from(rejection);
    assert_eq!(error.code, "invalid_date_range");
    assert_eq!(error.message, "start must be on or before end");
    assert_eq!(error.details, Some(json!({"start": "2024-01-19"})));
}
