//! Request specifications for the frozen lake routes.

use serde_json::{json, Value};

/// One symbol/data-type window ensure request.
///
/// The lake plans and resumes its own fetches server-side, so a request always
/// spans the whole window and a repeat request continues an interrupted
/// ensure. `interval` is the bars partition token (`1min`, `1d`) and is
/// omitted for the other data types.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnsureSpec {
    /// Requested asset class.
    pub asset_class: String,
    /// Requested data type.
    pub data_type: String,
    /// Requested symbol.
    pub symbol: String,
    /// Inclusive first session date (`YYYY-MM-DD`).
    pub start_date: String,
    /// Inclusive last session date (`YYYY-MM-DD`).
    pub end_date: String,
    /// Bars interval token, when the data type is `bars`.
    pub interval: Option<String>,
}

impl EnsureSpec {
    /// Build a window spec without an interval.
    #[must_use]
    pub fn new(
        asset_class: &str,
        data_type: &str,
        symbol: &str,
        start_date: &str,
        end_date: &str,
    ) -> Self {
        Self {
            asset_class: asset_class.to_string(),
            data_type: data_type.to_string(),
            symbol: symbol.to_string(),
            start_date: start_date.to_string(),
            end_date: end_date.to_string(),
            interval: None,
        }
    }

    /// Attach the bars interval token.
    #[must_use]
    pub fn with_interval(mut self, interval: &str) -> Self {
        self.interval = Some(interval.to_string());
        self
    }

    /// The `POST /v1/catalog/ensure` body.
    pub(crate) fn ensure_body(&self) -> Value {
        let mut body = serde_json::Map::new();
        body.insert("asset_class".to_string(), json!(self.asset_class));
        body.insert("data_type".to_string(), json!(self.data_type));
        body.insert("symbol".to_string(), json!(self.symbol));
        body.insert("start_date".to_string(), json!(self.start_date));
        body.insert("end_date".to_string(), json!(self.end_date));
        if let Some(interval) = &self.interval {
            body.insert("interval".to_string(), json!(interval));
        }
        Value::Object(body)
    }

    /// The `GET /v1/catalog/coverage` query parameters.
    pub(crate) fn query_params(&self) -> Vec<(&'static str, String)> {
        let mut params = vec![
            ("asset_class", self.asset_class.clone()),
            ("data_type", self.data_type.clone()),
            ("symbol", self.symbol.clone()),
            ("start_date", self.start_date.clone()),
            ("end_date", self.end_date.clone()),
        ];
        if let Some(interval) = &self.interval {
            params.push(("interval", interval.clone()));
        }
        params
    }
}

/// One ensured dataset read request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadSpec {
    /// Requested asset class.
    pub asset_class: String,
    /// Requested data type.
    pub data_type: String,
    /// Requested symbol.
    pub symbol: String,
    /// Inclusive first session date (`YYYY-MM-DD`).
    pub start_date: String,
    /// Inclusive last session date (`YYYY-MM-DD`).
    pub end_date: String,
    /// Bars interval token, when the data type is `bars`.
    pub interval: Option<String>,
}

impl ReadSpec {
    /// Build a window spec without an interval.
    #[must_use]
    pub fn new(
        asset_class: &str,
        data_type: &str,
        symbol: &str,
        start_date: &str,
        end_date: &str,
    ) -> Self {
        Self {
            asset_class: asset_class.to_string(),
            data_type: data_type.to_string(),
            symbol: symbol.to_string(),
            start_date: start_date.to_string(),
            end_date: end_date.to_string(),
            interval: None,
        }
    }

    /// Attach the bars interval token.
    #[must_use]
    pub fn with_interval(mut self, interval: &str) -> Self {
        self.interval = Some(interval.to_string());
        self
    }

    /// The `GET /v1/snapshot` query parameters.
    pub(crate) fn query_params(&self) -> Vec<(&'static str, String)> {
        let mut params = vec![
            ("asset_class", self.asset_class.clone()),
            ("data_type", self.data_type.clone()),
            ("symbol", self.symbol.clone()),
            ("start_date", self.start_date.clone()),
            ("end_date", self.end_date.clone()),
        ];
        if let Some(interval) = &self.interval {
            params.push(("interval", interval.clone()));
        }
        params
    }
}
