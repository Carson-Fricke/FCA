//! Native-engine plugin adapter for platform study programs.
//!
//! A study replays history to fit and observe a research model and to resolve
//! indicators. It has **no** order, position, sizing, portfolio, or venue
//! capability: the runtime never exposes the orders surface and never returns
//! engine orders. The workspace model source receives only plain data mappings
//! through the research program, and a study is recorded with the `study` shell
//! and a `study.json` artifact rather than a backtest.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::{
    BacktestOutcome, EngineFill, EngineMarket, EngineOptions, EngineOrder, StrategyInstance,
    StrategyRuntime, StrategyStart,
};
use backtest::backtest::errors::BacktestError;
use backtest::backtest::job::run_native_study_job_with_data_dir;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::backtest::{market_to_python, parameters_from_json};
use crate::context::{PyEngineContext, build_context};
use crate::error::HostError;
use crate::python::{default_search_paths, install_marketdata_shim, install_search_paths};

/// A Python study program driven through the native engine plugin seam.
pub struct PyStudyRuntime {
    module: String,
    class: String,
    parameters: BTreeMap<String, serde_json::Value>,
    search_paths: Vec<PathBuf>,
    program: Option<Py<PyAny>>,
    context: Option<Py<PyEngineContext>>,
}

impl PyStudyRuntime {
    /// Build an unstarted runtime for one configured `module:Class` study program.
    ///
    /// # Errors
    ///
    /// Returns [`HostError`] when the implementation path is malformed.
    pub fn new(
        implementation: &str,
        parameters: BTreeMap<String, serde_json::Value>,
    ) -> Result<Self, HostError> {
        let (module, class) = implementation.rsplit_once(':').ok_or_else(|| {
            HostError::Python("study program implementation must use module:Class syntax".to_string())
        })?;
        let mut search_paths = Vec::new();
        if let Some(root) = crate::python::workspace_root() {
            search_paths.push(root);
        }
        search_paths.extend(default_search_paths());
        Ok(Self {
            module: module.to_string(),
            class: class.to_string(),
            parameters,
            search_paths,
            program: None,
            context: None,
        })
    }

    fn build(&mut self, start: &StrategyStart) -> Result<(), BacktestError> {
        let module = self.module.clone();
        let class = self.class.clone();
        let parameters = self.parameters.clone();
        let search_paths = self.search_paths.clone();
        let symbols = start.symbols.clone();
        let now_ns = start.start_ns;
        let book_id = start.instance_id.clone();
        let initial_cash = start.initial_cash;
        let seed = start.seed;

        let built: Result<(Py<PyAny>, Py<PyEngineContext>), HostError> = Python::attach(|py| {
            install_search_paths(py, &search_paths)?;
            install_marketdata_shim(py)?;
            // The study context exists only so the program can publish metrics;
            // the orders surface is never handed to the study program.
            let (context, _clock, _orders, _books) =
                build_context(py, symbols, now_ns, book_id, initial_cash, seed)?;
            let module_obj = py.import(module.as_str())?;
            let class_obj = module_obj.getattr(class.as_str())?;
            let kwargs = PyDict::new(py);
            for (name, value) in &parameters {
                kwargs.set_item(name, crate::python::json_loads(py, &value.to_string())?)?;
            }
            let instance = class_obj.call((), Some(&kwargs))?;
            Ok((instance.unbind(), context))
        });
        let (program, context) = built.map_err(study_host_to_backtest)?;
        self.program = Some(program);
        self.context = Some(context);
        Ok(())
    }
}

impl StrategyRuntime for PyStudyRuntime {
    fn on_start(&mut self, start: &StrategyStart) -> Result<(), BacktestError> {
        self.build(start)
    }

    fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        let program = self.program.as_ref().ok_or_else(|| {
            BacktestError::StrategyExecution("study program not started".to_string())
        })?;
        let result: Result<(), HostError> = Python::attach(|py| {
            let callback = match event {
                EngineMarket::Quote { .. } => "on_quote",
                EngineMarket::Trade { .. } => "on_trade",
            };
            let py_event = market_to_python(py, event)?;
            program.bind(py).call_method1(callback, (py_event,))?;
            Ok(())
        });
        result.map_err(study_host_to_backtest)?;
        // A study never submits orders.
        Ok(Vec::new())
    }

    fn on_fill(&mut self, _fill: &EngineFill) -> Result<(), BacktestError> {
        Ok(())
    }

    fn on_stop(&mut self) -> Result<(), BacktestError> {
        let program = self.program.as_ref().ok_or_else(|| {
            BacktestError::StrategyExecution("study program not started".to_string())
        })?;
        let context = self.context.as_ref().ok_or_else(|| {
            BacktestError::StrategyExecution("study context not built".to_string())
        })?;
        let result: Result<(), HostError> = Python::attach(|py| {
            program
                .bind(py)
                .call_method1("on_stop", (context.bind(py),))?;
            Ok(())
        });
        result.map_err(study_host_to_backtest)
    }

    fn published_metrics(&self) -> BTreeMap<String, String> {
        self.context
            .as_ref()
            .map_or_else(BTreeMap::new, |context| {
                Python::attach(|py| context.bind(py).borrow().published_metrics())
            })
    }
}

fn study_host_to_backtest(error: HostError) -> BacktestError {
    BacktestError::StrategyExecution(error.to_string())
}

/// Build one Python-backed study runtime per configured study program.
///
/// # Errors
///
/// Returns [`HostError`] when a study program implementation path is malformed.
pub fn build_study_instances(
    configuration: &BacktestConfig,
) -> Result<Vec<StrategyInstance>, HostError> {
    let mut instances = Vec::new();
    for configured in &configuration.strategy_instances {
        let runtime =
            PyStudyRuntime::new(&configured.implementation, parameters_from_json(&configured.parameters))?;
        instances.push(StrategyInstance {
            instance_id: configured.instance_id.clone(),
            runtime: Box::new(runtime),
        });
    }
    Ok(instances)
}

/// Run one already-validated study configuration with platform study programs.
///
/// # Errors
///
/// Returns [`HostError`] when a study program cannot be built or the engine
/// rejects the run.
pub fn run_study_config(
    configuration: &BacktestConfig,
    output_dir: Option<&Path>,
    data_dir: Option<&Path>,
) -> Result<(BacktestOutcome, PathBuf), HostError> {
    let _python_owner = crate::python::PYTHON_ENGINE_LOCK
        .lock()
        .map_err(|_| HostError::Invalid("embedded Python engine lock is poisoned".to_string()))?;
    let instances = build_study_instances(configuration)?;
    run_native_study_job_with_data_dir(
        configuration,
        &EngineOptions::default(),
        output_dir,
        instances,
        data_dir,
    )
    .map_err(|error| HostError::Invalid(error.to_string()))
}
