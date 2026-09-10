//! Embedded Python host for user strategies, indicators, and screeners.
//!
//! This crate is the PyO3 boundary between the Rust engine and user-authored
//! Python code. It embeds one interpreter and exposes three host surfaces:
//!
//! * [`StrategyHost`] loads unmodified strategy classes from `strategies/`,
//!   instantiates them with validated parameters (including the
//!   `parameters=` mapping convention), and dispatches lifecycle callbacks.
//! * [`IndicatorHost`] loads and calls user-authored Python indicators with a
//!   batched hot path.
//! * [`ScreenerHost`] executes user screener programs and resolves their
//!   `screen` entry point.
//!
//! Every surface performs its Python work inside a single interpreter
//! acquisition per call (or per batch), so hot loops do not pay a per-event
//! GIL acquisition.

pub mod binding;
pub mod contracts;
pub mod error;
pub mod indicator;
pub mod lint;
pub mod python;
pub mod reference;
pub mod screener;
pub mod strategy;

pub use binding::StrategyBinding;
pub use contracts::{
    callback_name_for_event, is_known_callback, require_callbacks, validate_callback_order,
    ContractError, ContractErrorKind, CALLBACK_ORDER,
};
pub use error::HostError;
pub use indicator::{IndicatorHost, LoadedCallable};
pub use lint::{
    default_lint_path, LintReport, PythonStrategyLinter, SandboxViolation, StrategyLintError,
    ViolationCategory,
};
pub use reference::ReferenceStrategy;
pub use screener::{LoadedScreener, ScreenerHost, SCREEN_ENTRYPOINT};
pub use strategy::{LoadedStrategy, StrategyEvent, StrategyHost, StrategySpec};

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    static COUNTER: AtomicUsize = AtomicUsize::new(0);

    fn temp_dir(tag: &str) -> PathBuf {
        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!(
            "harness-host-{}-{tag}-{unique}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    fn unique_module(tag: &str) -> String {
        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        format!("demo_{tag}_{}_{unique}", std::process::id())
    }

    const STRATEGY_SOURCE: &str = r#"
class Demo:
    def __init__(self, parameters=None):
        self.parameters = parameters or {}
        self.quotes = []
        self.started = False

    def on_start(self, context):
        self.started = True

    def on_quote(self, event):
        self.quotes.append(event["price"])

    def on_stop(self, context):
        self.started = False

    def summary(self):
        return {
            "threshold": self.parameters.get("threshold"),
            "quotes": self.quotes,
            "started": self.started,
        }
"#;

    #[test]
    fn loads_and_dispatches_a_user_strategy() {
        let dir = temp_dir("strategy");
        let module = unique_module("strategy");
        fs::write(dir.join(format!("{module}.py")), STRATEGY_SOURCE).expect("write module");
        let host = StrategyHost::with_search_paths(vec![dir.clone()]);
        let spec =
            StrategySpec::new(module, "Demo").with_parameter("threshold", serde_json::json!(0.5));
        let strategy = host.load(&spec).expect("load strategy");
        assert_eq!(strategy.class(), "Demo");
        assert!(strategy.has_callback("on_quote").expect("has"));

        strategy.call("on_start", Some("{}")).expect("on_start");
        let events = vec![
            StrategyEvent::new("on_quote", r#"{"price":1.0}"#),
            StrategyEvent::new("on_trade", r#"{"price":99.0}"#),
            StrategyEvent::new("on_quote", r#"{"price":2.0}"#),
        ];
        let dispatched = strategy.dispatch_batch(&events).expect("dispatch");
        assert_eq!(dispatched, 2, "only on_quote exists");

        let summary = strategy.call_json("summary", None).expect("summary");
        assert!(summary.contains("\"threshold\": 0.5"), "{summary}");
        assert!(summary.contains("[1.0, 2.0]"), "{summary}");
        assert!(summary.contains("\"started\": true"), "{summary}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn strategy_parameters_are_keyword_arguments_when_accepted() {
        let dir = temp_dir("kwargs");
        let module = unique_module("kwargs");
        fs::write(
            dir.join(format!("{module}.py")),
            "class Echo:\n    def __init__(self, threshold):\n        self.threshold = threshold\n    def on_start(self, context):\n        pass\n    def on_stop(self, context):\n        pass\n    def summary(self):\n        return {\"threshold\": self.threshold}\n",
        )
        .expect("write module");
        let host = StrategyHost::with_search_paths(vec![dir.clone()]);
        let spec =
            StrategySpec::new(module, "Echo").with_parameter("threshold", serde_json::json!(7));
        let strategy = host.load(&spec).expect("load");
        let summary = strategy.call_json("summary", None).expect("summary");
        assert!(summary.contains("\"threshold\": 7"), "{summary}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn loads_and_calls_user_indicators() {
        let dir = temp_dir("indicator");
        let module = unique_module("indicator");
        fs::write(
            dir.join(format!("{module}.py")),
            "def scale(values):\n    return [value * 2 for value in values]\n\ndef last(values):\n    return values[-1]\n",
        )
        .expect("write module");
        let host = IndicatorHost::with_search_paths(vec![dir.clone()]);
        let scale = host.load(&module, "scale").expect("load scale");
        assert_eq!(
            scale.call_f64(&[1.0, 2.0, 3.0]).expect("call"),
            vec![2.0, 4.0, 6.0]
        );
        let batched = scale
            .call_f64_batch(&[vec![1.0], vec![2.0, 3.0]])
            .expect("batch");
        assert_eq!(batched, vec![vec![2.0], vec![4.0, 6.0]]);

        let last = host.load(&module, "last").expect("load last");
        assert_eq!(last.call_f64(&[5.0, 6.0]).expect("call"), vec![6.0]);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn executes_user_screener_programs() {
        let dir = temp_dir("screener");
        let path = dir.join("demo_screen.py");
        fs::write(
            &path,
            "def screen(context):\n    return {\"count\": len(context[\"symbols\"]), \"first\": context[\"symbols\"][0]}\n",
        )
        .expect("write screener");
        let host = ScreenerHost::with_search_paths(vec![dir.clone()]);
        let screener = host.load_program(&path).expect("load program");
        let output = screener.run(r#"{"symbols":["A","B","C"]}"#).expect("run");
        assert!(output.contains("\"count\": 3"), "{output}");
        assert!(output.contains("\"first\": \"A\""), "{output}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn missing_screener_entrypoint_is_rejected() {
        let dir = temp_dir("screener-missing");
        let path = dir.join("empty_screen.py");
        fs::write(&path, "value = 1\n").expect("write");
        let host = ScreenerHost::new();
        let error = host.load_program(&path).err().expect("error");
        assert!(matches!(error, HostError::Python(_)), "{error}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn binding_lifecycle_matches_python_contract() {
        let dir = temp_dir("binding");
        let module = unique_module("binding");
        fs::write(
            dir.join(format!("{module}.py")),
            "class Demo:\n    def __init__(self, parameters=None):\n        self.calls = []\n    def on_start(self, context):\n        self.calls.append(\"on_start\")\n    def on_quote(self, event):\n        self.calls.append(\"on_quote\")\n    def on_stop(self, context):\n        self.calls.append(\"on_stop\")\n    def summary(self):\n        return self.calls\n",
        )
        .expect("write module");
        let host = StrategyHost::with_search_paths(vec![dir.clone()]);
        let strategy = host.load(&StrategySpec::new(module, "Demo")).expect("load");
        let mut binding = StrategyBinding::new(strategy, "{}").expect("bind");
        assert!(!binding.started());
        binding.start().expect("start");
        assert!(binding.started());
        assert!(binding.receive("quote", r#"{"price":1}"#).expect("receive"));
        let second_start = binding.start().expect_err("double start");
        assert_eq!(second_start.kind, ContractErrorKind::Runtime);
        binding.stop().expect("stop");
        assert!(!binding.receive("quote", "{}").expect("after stop"));
        let summary = binding
            .strategy()
            .call_json("summary", None)
            .expect("summary");
        assert_eq!(summary, r#"["on_start", "on_quote", "on_stop"]"#);
        let _ = fs::remove_dir_all(dir);
    }
}
