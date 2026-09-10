//! Rust surface for the strategy sandbox lint.
//!
//! The lint rules and their AST walker live in
//! `harness.strategy.lint`; the host embeds the interpreter, so this module
//! bridges that exact implementation across the boundary and returns native
//! Rust diagnostics. Behavior (codes, categories, ordering, remediation text)
//! is preserved because the Python checker is the single source of truth
//! until the AST walker itself is ported.

use std::fmt;
use std::path::{Path, PathBuf};

use pyo3::prelude::*;
use pyo3::types::PyDict;

/// One class of determinism-breaking strategy behavior.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ViolationCategory {
    /// Wall-clock reads.
    WallClock,
    /// Sleeping.
    Sleeping,
    /// Networking.
    Networking,
    /// Subprocesses.
    Subprocess,
    /// Unseeded randomness.
    UnseededRandom,
    /// Asyncio and async syntax.
    Asyncio,
    /// Concurrency.
    Concurrency,
}

impl ViolationCategory {
    /// Parse the Python wire spelling.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "wall-clock" => Some(Self::WallClock),
            "sleeping" => Some(Self::Sleeping),
            "networking" => Some(Self::Networking),
            "subprocess" => Some(Self::Subprocess),
            "unseeded-random" => Some(Self::UnseededRandom),
            "asyncio" => Some(Self::Asyncio),
            "concurrency" => Some(Self::Concurrency),
            _ => None,
        }
    }

    /// The Python wire spelling.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::WallClock => "wall-clock",
            Self::Sleeping => "sleeping",
            Self::Networking => "networking",
            Self::Subprocess => "subprocess",
            Self::UnseededRandom => "unseeded-random",
            Self::Asyncio => "asyncio",
            Self::Concurrency => "concurrency",
        }
    }
}

impl fmt::Display for ViolationCategory {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// One actionable sandbox diagnostic.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxViolation {
    /// Source file name.
    pub filename: String,
    /// One-based line number.
    pub lineno: i64,
    /// Zero-based column offset.
    pub col_offset: i64,
    /// Rule code (for example `SB001`).
    pub code: String,
    /// Rule category.
    pub category: ViolationCategory,
    /// Offending symbol.
    pub symbol: String,
    /// Human-readable message.
    pub message: String,
    /// Suggested remediation.
    pub remediation: String,
}

impl SandboxViolation {
    /// The `file:line:column` location.
    #[must_use]
    pub fn location(&self) -> String {
        format!("{}:{}:{}", self.filename, self.lineno, self.col_offset)
    }

    /// Render one diagnostic line naming the rule and its remedy.
    #[must_use]
    pub fn format(&self) -> String {
        format!(
            "{}: {} [{}] {} (fix: {})",
            self.location(),
            self.code,
            self.category,
            self.message,
            self.remediation
        )
    }
}

/// All sandbox violations found in one linted target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LintReport {
    /// Linted file name.
    pub filename: String,
    /// Violations in source order.
    pub violations: Vec<SandboxViolation>,
}

impl LintReport {
    /// Whether the target stays inside the sandbox.
    #[must_use]
    pub fn ok(&self) -> bool {
        self.violations.is_empty()
    }

    /// The rule code of every violation in report order.
    #[must_use]
    pub fn codes(&self) -> Vec<&str> {
        self.violations.iter().map(|v| v.code.as_str()).collect()
    }

    /// Render a human-readable report for console output.
    #[must_use]
    pub fn render(&self) -> String {
        if self.violations.is_empty() {
            return format!("{}: strategy sandbox clean", self.filename);
        }
        let header = format!(
            "{}: {} sandbox violation(s)",
            self.filename,
            self.violations.len()
        );
        let mut lines = vec![header];
        lines.extend(self.violations.iter().map(|v| format!("  {}", v.format())));
        lines.join("\n")
    }
}

/// A strategy target that cannot be linted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrategyLintError(pub String);

impl fmt::Display for StrategyLintError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for StrategyLintError {}

/// The Python-backed sandbox linter.
#[derive(Debug, Clone)]
pub struct PythonStrategyLinter {
    lint_path: PathBuf,
}

impl PythonStrategyLinter {
    /// Compose a linter from the path to `harness/strategy/lint.py`.
    #[must_use]
    pub fn new(lint_path: impl Into<PathBuf>) -> Self {
        Self {
            lint_path: lint_path.into(),
        }
    }

    /// The configured lint module path.
    #[must_use]
    pub fn lint_path(&self) -> &Path {
        &self.lint_path
    }

    /// Lint strategy source text and return every sandbox violation.
    ///
    /// # Errors
    ///
    /// Returns [`StrategyLintError`] when the lint module cannot be loaded or
    /// the source is not valid Python.
    pub fn lint_source(
        &self,
        source: &str,
        filename: Option<&str>,
    ) -> Result<LintReport, StrategyLintError> {
        let lint_path = self.lint_path.to_string_lossy().to_string();
        let result: PyResult<LintReport> = Python::attach(|py| {
            let importlib = py.import("importlib.util")?;
            let spec = importlib
                .call_method1(
                    "spec_from_file_location",
                    ("_harness_strategy_lint", lint_path.as_str()),
                )?;
            let module = importlib.call_method1("module_from_spec", (&spec,))?;
            py.import("sys")?
                .getattr("modules")?
                .set_item("_harness_strategy_lint", &module)?;
            spec.getattr("loader")?
                .call_method1("exec_module", (&module,))?;

            let kwargs = PyDict::new(py);
            if let Some(filename) = filename {
                kwargs.set_item("filename", filename)?;
            }
            let report = module.call_method("lint_strategy_source", (source,), Some(&kwargs))?;

            let report_filename: String = report.getattr("filename")?.extract()?;
            let mut violations = Vec::new();
            for item in report.getattr("violations")?.try_iter()? {
                let item = item?;
                let category_text: String = item.getattr("category")?.extract()?;
                violations.push(SandboxViolation {
                    filename: item.getattr("filename")?.extract()?,
                    lineno: item.getattr("lineno")?.extract()?,
                    col_offset: item.getattr("col_offset")?.extract()?,
                    code: item.getattr("code")?.extract()?,
                    category: ViolationCategory::parse(&category_text)
                        .unwrap_or(ViolationCategory::WallClock),
                    symbol: item.getattr("symbol")?.extract()?,
                    message: item.getattr("message")?.extract()?,
                    remediation: item.getattr("remediation")?.extract()?,
                });
            }
            Ok(LintReport {
                filename: report_filename,
                violations,
            })
        });
        result.map_err(|error| StrategyLintError(error.to_string()))
    }

    /// Lint a strategy file and return every sandbox violation.
    ///
    /// # Errors
    ///
    /// Returns [`StrategyLintError`] when the file cannot be read or linted.
    pub fn lint_file(&self, path: &Path) -> Result<LintReport, StrategyLintError> {
        let source = std::fs::read_to_string(path)
            .map_err(|error| StrategyLintError(error.to_string()))?;
        self.lint_source(&source, Some(&path.to_string_lossy()))
    }

    /// Lint source and raise when it is not clean.
    ///
    /// # Errors
    ///
    /// Returns [`StrategyLintError`] when the target is unclean.
    pub fn assert_clean(
        &self,
        source: &str,
        filename: Option<&str>,
    ) -> Result<LintReport, StrategyLintError> {
        let report = self.lint_source(source, filename)?;
        if report.ok() {
            Ok(report)
        } else {
            Err(StrategyLintError(report.render()))
        }
    }
}

/// Return the default lint module path relative to this crate.
#[must_use]
pub fn default_lint_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("src")
        .join("harness")
        .join("strategy")
        .join("lint.py")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_strategy_source_produces_no_violations() {
        let linter = PythonStrategyLinter::new(default_lint_path());
        let report = linter
            .lint_source("def on_quote(event):\n    return event\n", Some("<test>"))
            .expect("lint");
        assert!(report.ok(), "{}", report.render());
        assert!(report.render().contains("clean"));
    }

    #[test]
    fn wall_clock_reads_are_reported() {
        let linter = PythonStrategyLinter::new(default_lint_path());
        let report = linter
            .lint_source(
                "import time\n\ndef on_quote(event):\n    return time.time()\n",
                Some("<test>"),
            )
            .expect("lint");
        assert!(!report.ok());
        assert!(report.codes().contains(&"SB001"), "{}", report.render());
        assert_eq!(report.violations[0].category, ViolationCategory::WallClock);
    }

    #[test]
    fn async_syntax_is_reported() {
        let linter = PythonStrategyLinter::new(default_lint_path());
        let report = linter
            .lint_source("async def on_quote(event):\n    return event\n", Some("<test>"))
            .expect("lint");
        assert!(report.codes().contains(&"SB700"), "{}", report.render());
        assert_eq!(report.violations[0].category, ViolationCategory::Asyncio);
    }

    #[test]
    fn assert_clean_rejects_violations() {
        let linter = PythonStrategyLinter::new(default_lint_path());
        let error = linter
            .assert_clean("import time\nvalue = time.time()\n", Some("<test>"))
            .expect_err("error");
        assert!(error.0.contains("SB001"));
    }
}
