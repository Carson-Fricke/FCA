//! Owns MCP discovery schemas and tool registration; delegates execution to handlers.
use super::*;
type ToolHandler =
    Arc<dyn Fn(&Map<String, Value>) -> Result<Value, ToolExecutionError> + Send + Sync>;

/// One MCP tool: name, contract, and dispatch target.
pub struct Tool {
    /// Tool name.
    pub name: String,
    /// Human-readable description.
    pub description: String,
    /// JSON input schema.
    pub input_schema: Value,
    /// Capability that publishes this tool.
    pub capability: &'static str,
    read_only: bool,
    handler: ToolHandler,
}

impl Tool {
    /// Compose a tool from its contract and dispatch target.
    #[must_use]
    pub fn new<F>(
        name: &str,
        description: &str,
        input_schema: Value,
        capability: &'static str,
        handler: F,
    ) -> Self
    where
        F: Fn(&Map<String, Value>) -> Result<Value, ToolExecutionError> + Send + Sync + 'static,
    {
        Self {
            name: name.to_string(),
            description: description.to_string(),
            input_schema,
            capability,
            read_only: false,
            handler: Arc::new(handler),
        }
    }

    /// Declare a read operation at its registration boundary.
    #[must_use]
    pub fn read_only(mut self) -> Self {
        self.read_only = true;
        self
    }

    /// The `tools/list` entry for this tool.
    #[must_use]
    pub fn schema_entry(&self) -> Value {
        json!({
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": self.read_only},
        })
    }

    /// Dispatch one tool call with already-validated arguments.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] for domain-level failures.
    pub fn call(&self, arguments: &Map<String, Value>) -> Result<Value, ToolExecutionError> {
        (self.handler)(arguments)
    }
}

/// Immutable, capability-filtered tool registry.
pub struct ToolCatalog {
    tools: Vec<Tool>,
}

impl ToolCatalog {
    /// Compose a catalog, rejecting duplicate names.
    ///
    /// # Errors
    ///
    /// Returns [`ContextError`] when tool names are duplicated.
    pub fn new(tools: Vec<Tool>) -> Result<Self, ContextError> {
        let mut names: Vec<&str> = tools.iter().map(|tool| tool.name.as_str()).collect();
        names.sort_unstable();
        names.dedup();
        if names.len() != tools.len() {
            return Err(ContextError("tool names must be unique".to_string()));
        }
        Ok(Self { tools })
    }

    /// Return the named tool, or `None` when not published.
    #[must_use]
    pub fn resolve(&self, name: &str) -> Option<&Tool> {
        self.tools.iter().find(|tool| tool.name == name)
    }

    /// Return the published tool names in catalog order.
    #[must_use]
    pub fn names(&self) -> Vec<&str> {
        self.tools.iter().map(|tool| tool.name.as_str()).collect()
    }

    /// Return the published tools in catalog order.
    #[must_use]
    pub fn tools(&self) -> &[Tool] {
        &self.tools
    }
}

fn text_property() -> Value {
    json!({"type": "string", "minLength": 1, "maxLength": IDENTIFIER_MAX})
}

fn study_subscription_schema() -> Value {
    let symbols = json!({"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1, "pattern": "^\\S(?:.*\\S)?$"}});
    json!({
        "type": "array", "minItems": 1,
        "description": "Exact requested inputs, matching the program and its indicators. Bars support multiple session-anchored minute/hour intervals and 1d; quote/trade must omit interval. Symbol aliases must equal the subscription symbol union; duplicate aliases must agree. Symbols must be nonempty and unpadded.",
        "items": {"oneOf": [
            {"type": "object", "additionalProperties": false,
             "required": ["data_type", "symbols", "interval"],
             "properties": {"data_type": {"const": "bars"}, "symbols": symbols,
                            "interval": {"type": "string", "pattern": "^(?:[1-9][0-9]*(?:min|h)|1d)$"}}},
            {"type": "object", "additionalProperties": false,
             "required": ["data_type", "symbols"],
             "properties": {"data_type": {"type": "string", "enum": ["quote", "trade"]}, "symbols": symbols}}
        ]}
    })
}

fn study_configuration_schema() -> Value {
    let symbols =
        json!({"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}});
    let data = json!({
        "type": "object", "additionalProperties": false,
        "required": ["subscriptions"],
        "properties": {
            "snapshot_id": {"type": "string", "minLength": 1},
            "fingerprint": {"type": "string"},
            "subscriptions": study_subscription_schema(),
            "symbols": symbols
        }
    });
    json!({
        "type": "object",
        "required": ["start", "end"],
        "anyOf": [
            {"required": ["subscriptions"]},
            {"required": ["data"]}
        ],
        "description": "Supported replay configuration. Declare exact subscriptions; bars support multiple completed minute/hour intervals and 1d, tick-only inputs omit interval, and symbol aliases must equal the subscription union.",
        "properties": {
            "start": {"type": "string", "format": "date-time", "pattern": "(Z|[+-][0-9]{2}:[0-9]{2})$", "description": "RFC 3339 timestamp with Z or explicit UTC offset; must precede end."},
            "end": {"type": "string", "format": "date-time", "pattern": "(Z|[+-][0-9]{2}:[0-9]{2})$", "description": "RFC 3339 timestamp with Z or explicit UTC offset; must follow start."},
            "subscriptions": study_subscription_schema(),
            "requiredSymbols": symbols,
            "required_symbols": symbols,
            "symbols": symbols,
            "data": data
        }
    })
}

fn authored_model_schema() -> Value {
    json!({
        "type": "object", "required": ["source", "harnessConfiguration"],
        "properties": {
            "modelId": {"type": "string", "minLength": 1, "maxLength": 128},
            "version": {"type": "string", "minLength": 1, "maxLength": 64},
            "description": {"type": "string", "minLength": 1},
            "source": {"type": "string", "minLength": 1},
            "outputs": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "tests": {"type": "object"},
            "harnessConfiguration": study_configuration_schema()
        }
    })
}

/// Build the capability-filtered tool catalog for one composition.
///
/// # Errors
///
/// Returns [`ContextError`] when the catalog cannot be composed.
pub fn build_catalog(context: HarnessToolsContext) -> Result<ToolCatalog, ContextError> {
    let capabilities = context.capabilities;
    let tools = Arc::new(super::compose(context)?);
    let mut candidates: Vec<Tool> = Vec::new();
    if capabilities.study || capabilities.backtest {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_READ_SDK_REFERENCE,
            "Read the serving harness compiler's SDK identity/version/ABI and source hashes. \
             Omit topic for metadata only; topic reads bounded current SDK source, not a local \
             package or stale template. Compare sdkIdentity before using local SDK guidance. \
             Refresh initialize/serverInfo to compare current validationIdentity before running. \
             Ordinary topics are returned whole; large sources use cl100k_base token budgets. Follow nextArguments exactly until null.",
            json!({"type": "object", "additionalProperties": false, "properties": {
                "topic": {"type": "string", "description": "SDK alias or source path from sourceHashes; omit topic to discover topics."},
                "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1},
                "token_offset": {"type": "integer", "minimum": 0},
                "max_tokens": {"type": "integer", "minimum": 32, "maximum": 65536}
            }}), "sdk", move |arguments| handler.validation.read_sdk_reference(arguments),
        ).read_only());
    }

    if capabilities.backtest {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_RUN_BACKTEST,
            "Run one deterministic backtest: durable local submission, \
             or a bearer-authenticated POST to the remote harness API returning {job_id, state, kind}. \
             config_path must reference an existing, readable configuration document; \
             do not guess paths.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "anyOf": [
                    {"required": ["config_path"]},
                    {"required": ["config"]}
                ],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to an existing backtest configuration document."
                    },
                    "config": {
                        "type": "object",
                        "description": "An inline backtest configuration document, used when no config file exists. Same schema as the configuration file; call backtest_config_template for a starter document."
                    },
                    "job_id": text_property(),
                    "workspace": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Local output directory passed as --output-dir; ignored when the remote API executes the launch."
                    }
                }
            }),
            "backtest",
            move |arguments| handler.launch.run_backtest(arguments),
        ));
    }
    if capabilities.study {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_RUN_STUDY,
            "Run one deterministic study: durable local submission, or a \
             bearer-authenticated POST to the remote harness API returning {job_id, state, kind}. \
             A study replays history to fit and observe a research model and resolve \
             indicators with no order execution; it is never recorded as a backtest. \
             config_path must reference an existing, readable study configuration document; \
             do not guess paths.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "anyOf": [
                    {"required": ["config_path"]},
                    {"required": ["config"]}
                ],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to an existing study configuration document."
                    },
                    "config": {
                        "type": "object",
                        "description": "An inline study configuration document, used when no config file exists."
                    },
                    "job_id": text_property(),
                    "workspace": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Local output directory passed as --output-dir; ignored when the remote API executes the launch."
                    }
                }
            }),
            "study",
            move |arguments| handler.launch.run_study(arguments),
        ));
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_VALIDATE_STUDY,
            "Validate the study model you are drafting without replaying \
             history: compile, load, and construct its authored Rust program \
             over the managed JIT workspace and return compact structured \
             diagnostics. Pass the model object itself under 'model' - the same \
             object the completion contract carries. model.source and \
             model.harnessConfiguration.start/.end are required RFC 3339 timestamps \
             with Z or an explicit offset (for example 2025-01-01T00:00:00-05:00), \
             with start before end. Declare exact subscriptions directly or under \
             data (data.subscriptions); interval-only configuration is rejected. \
             Use harness_strategy_sdk imports and its serde_json::Value re-export. \
             Implement MarketConsumer with type Context = ObservationContext and \
             fn subscriptions(&self) -> Vec<Subscription>; use Subscription::Bars \
             { symbols, interval }, Subscription::Quote { symbols }, or \
             Subscription::Trade { symbols }. Put market callbacks on MarketConsumer: \
             fn on_bar(&mut self, frame: &BarFrame, ctx: &mut ObservationContext), \
             on_quote(&mut self, event: &QuoteEvent, ctx: &mut ObservationContext), \
             on_trade(&mut self, event: &TradeEvent, ctx: &mut ObservationContext). \
             BarFrame has bars_by_symbol and missing_symbols, not a single bar. \
             Decimal fields are exact; use checked arithmetic, not primitive casts. \
             Implement StudyProgram::new(parameters: Value) -> Self and optionally \
             on_stop(&mut self, metrics: &mut MetricPublisher); metrics.publish(name, \
             value: f64) returns Result<(), PublishError>. Export at module scope \
             with harness_strategy_sdk::export_study_program!(MyStudy). A study has \
             no order sink. Keep subscriptions and indicator topology stable; \
             indicators update before their consumer. Validation does not replay \
             data or prove output behavior. Fix reported source/configuration \
             errors and call validate_study again before submitting the contract; \
             report infrastructure failures rather than treating them as source errors.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["model"],
                "properties": {
                    "model": authored_model_schema()
                }
            }),
            "study",
            move |arguments| handler.validation.validate_study(arguments),
        ));
    }
    if capabilities.screener {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_RUN_SCREENER,
            "Run one deterministic screening run: durable local submission, \
             or a bearer-authenticated POST to the remote harness API returning \
             {job_id, state, kind}. A screening run evaluates one JIT-authored \
             cross-sectional Rust program and produces a ranked 'screen.json' artifact; \
             market data arrives only through the harness data-source port, so an \
             unavailable lake fails the run loudly. config_path must reference an existing, \
             readable screen configuration document; do not guess paths. Use \
             screener_config_template for a starter document.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "anyOf": [
                    {"required": ["config_path"]},
                    {"required": ["config"]}
                ],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to an existing screen configuration document."
                    },
                    "config": {
                        "type": "object",
                        "description": "An inline screen configuration document, used when no config file exists. Same schema as the configuration file; call screener_config_template for a starter document."
                    },
                    "job_id": text_property(),
                    "workspace": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Local output directory passed as --output-dir; ignored when the remote API executes the launch."
                    }
                }
            }),
            "screener",
            move |arguments| handler.launch.run_screener(arguments),
        ));
    }
    if capabilities.backtest {
        let _handler = tools.clone();
        candidates.push(
            Tool::new(
                TOOL_BACKTEST_CONFIG_TEMPLATE,
                "Return a canonical starter backtest configuration document (YAML). \
             Adapt job_id, the time window, symbols, initial_cash, and the \
             strategy parameters, then pass the result as the inline `config` \
             argument of run_backtest when no configuration file exists.",
                json!({
                    "type": "object",
                    "additionalProperties": false,
                    "properties": {}
                }),
                "backtest",
                move |_arguments| templates::backtest_config_template(),
            )
            .read_only(),
        );
    }
    if capabilities.screener {
        let _handler = tools.clone();
        candidates.push(
            Tool::new(
                TOOL_SCREENER_CONFIG_TEMPLATE,
                "Return a canonical starter screening configuration document (YAML, \
             `shell: screen`) with an inline example program using \
             `export_screening_program!`. Adapt name, as_of, lookback_days, and \
             rank_metric, then recompute bundle_hash/source_sha256 for the edited source \
             and pass the result as the inline `config` argument of run_screener when no \
             configuration file exists.",
                json!({
                    "type": "object",
                    "additionalProperties": false,
                    "properties": {}
                }),
                "screener",
                move |_arguments| templates::screener_config_template(),
            )
            .read_only(),
        );
    }
    if capabilities.backtest {
        let handler = tools.clone();
        let input_schema = json!({
            "type": "object", "additionalProperties": false, "required": ["model"],
            "properties": {"model": authored_model_schema()}
        });
        candidates.push(Tool::new(
            TOOL_VALIDATE_STRATEGY,
            "Validate an authored Rust strategy without replaying data: compile, load, construct, \
             and check effective subscriptions (including indicators) against harnessConfiguration. \
             Pass {model: {source, harnessConfiguration}} as for validate_study; timestamps require \
             RFC 3339 with Z or an explicit offset; declare exact tagged \
             subscriptions directly or under data (interval-only configuration is \
             rejected). Implement MarketConsumer with type Context = \
             OrderSink, fn subscriptions(&self) -> Vec<Subscription>, and market callbacks on \
             MarketConsumer; implement StrategyProgram::new(parameters: \
             harness_strategy_sdk::serde_json::Value) -> Self. Export at module scope with \
             harness_strategy_sdk::export_strategy_program!(MyStrategy). Bar callbacks receive \
             &BarFrame with bars_by_symbol and missing_symbols. Subscriptions and indicator topology \
             must remain stable. This is not a backtest and does not prove trading performance. \
             Fix compact structured compiler/configuration errors and call validate_strategy again; \
             report infrastructure failures separately. A strategy run uses shell backtest and backtest.json.",
            input_schema,
            "backtest",
            move |arguments| handler.validation.validate_strategy(arguments),
        ));
    }
    if capabilities.job_status {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_JOB_STATUS,
            "Read one job's persisted state \
             {job_id, kind, state, created_at, updated_at, error_message} from the remote harness API.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["job_id"],
                "properties": {"job_id": text_property()}
            }),
            "job_status",
            move |arguments| handler.queries.get_job_status(arguments),
        ).read_only());
    }
    if capabilities.artifacts {
        let result_handler = tools.clone();
        candidates.push(
            Tool::new(
                TOOL_GET_JOB_RESULT,
                "Read one stored job's result record \
             {job_id, kind, state, strategy_id, metrics, artifact_count, ...} \
             from the remote harness API.",
                json!({
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["job_id"],
                    "properties": {"job_id": text_property()}
                }),
                "artifacts",
                move |arguments| result_handler.queries.get_job_result(arguments),
            )
            .read_only(),
        );
        let list_handler = tools.clone();
        candidates.push(
            Tool::new(
                TOOL_LIST_ARTIFACTS,
                "List one stored job's artifact manifest entries \
             {name, uri, sha256, sizeBytes} from the remote harness API.",
                json!({
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["job_id"],
                    "properties": {"job_id": text_property()}
                }),
                "artifacts",
                move |arguments| list_handler.queries.list_artifacts(arguments),
            )
            .read_only(),
        );
        let read_handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_READ_ARTIFACT,
            "Read one stored job artifact's contents as utf-8 text or hex-encoded bytes \
             {job_id, name, encoding, content}.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["job_id", "name"],
                "properties": {
                    "job_id": {"type": "string", "minLength": 1},
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Artifact name relative to the job, e.g. metrics.json or artifacts/equity.parquet."
                    }
                }
            }),
            "artifacts",
            move |arguments| read_handler.queries.read_artifact(arguments),
        ).read_only());
    }
    if capabilities.trader {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_LAUNCH_TRADER,
            "Run one supervised paper trading session through the local 'harness trader' \
             process; the exit code is the session outcome.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["config_path"],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to the trader configuration document."
                    }
                }
            }),
            "trader",
            move |arguments| handler.launch.launch_trader(arguments),
        ));
    }
    ToolCatalog::new(candidates)
}
