//! Native Rust strategy sandbox lint.
//!
//! The rule table and its AST walker mirror `harness.strategy.lint`. The walker
//! parses strategy source with the pure-Rust `rustpython-parser`, without
//! starting a Python interpreter, and reproduces the Python checker's
//! observable behavior: the same rule codes and categories, the same symbol
//! spelling, the same source ordering, and the same remediation text.
//!
//! Two resolution rules keep the checker predictable, exactly as in Python:
//!
//! * Prohibited *modules* are reported once, at their import statement.
//! * Prohibited *attributes* are reported at every use. An attribute chain that
//!   passes through a call result (`random.Random(7).random()`) is treated as a
//!   method on a returned object rather than a module attribute, so seeded
//!   generators and context boundaries stay legal.
//!
//! `harness.strategy.lint` remains the parity oracle: the Python suite in
//! `tests/strategy_lint` and this native walker are checked against the same
//! fixtures.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fmt;
use std::path::{Path, PathBuf};

use rustpython_parser::ast::{self, Ranged, Visitor};
use rustpython_parser::text_size::TextRange;
use rustpython_parser::Parse;

/// The filename reported when the caller does not supply one.
pub const DEFAULT_FILENAME: &str = "<strategy>";

/// One class of determinism-breaking strategy behavior.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
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

    /// The categories present in this report.
    #[must_use]
    pub fn categories(&self) -> BTreeSet<ViolationCategory> {
        self.violations.iter().map(|v| v.category).collect()
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

/// One prohibited module or module attribute.
///
/// `attributes == None` prohibits the whole module; otherwise only the listed
/// attribute names are prohibited while the module itself stays legal (for
/// example `time.sleep` versus `time.timezone`).
struct RuleSpec {
    code: &'static str,
    category: ViolationCategory,
    module: &'static str,
    attributes: Option<&'static [&'static str]>,
    summary: &'static str,
    remediation: &'static str,
}

const TIME_WALL_CLOCK: &[&str] = &[
    "clock_gettime",
    "clock_gettime_ns",
    "ctime",
    "gmtime",
    "localtime",
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
    "process_time",
    "process_time_ns",
    "thread_time",
    "thread_time_ns",
    "time",
    "time_ns",
];

const DATETIME_WALL_CLOCK: &[&str] = &[
    "now",
    "today",
    "utcnow",
    "utcfromtimestamp",
    "fromtimestamp",
];

const OS_WALL_CLOCK: &[&str] = &["times", "clock_gettime"];

const TIME_SLEEP: &[&str] = &["sleep"];

const OS_SUBPROCESS: &[&str] = &[
    "execl",
    "execle",
    "execlp",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "fork",
    "forkpty",
    "popen",
    "posix_spawn",
    "posix_spawnp",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "system",
];

const RANDOM_UNSEEDED: &[&str] = &[
    "SystemRandom",
    "betavariate",
    "choice",
    "choices",
    "expovariate",
    "gammavariate",
    "gauss",
    "getrandbits",
    "getstate",
    "lognormvariate",
    "normalvariate",
    "paretovariate",
    "randint",
    "random",
    "randrange",
    "sample",
    "seed",
    "setstate",
    "shuffle",
    "triangular",
    "uniform",
    "vonmisesvariate",
    "weibullvariate",
];

const OS_ENTROPY: &[&str] = &["urandom", "getrandom"];

const UUID_ENTROPY: &[&str] = &["uuid1", "uuid4"];

const NETWORK_REMEDIATION: &str = "read market data through context.data and \
    context.subscriptions instead of opening a connection";
const ASYNC_REMEDIATION: &str = "keep callbacks synchronous and schedule \
    timestamped events through context.scheduler";
const CONCURRENCY_REMEDIATION: &str = "order work deterministically through \
    context.scheduler and the event bus";
const RANDOM_REMEDIATION: &str = "use the seeded generator supplied by the \
    context, for example context.random.random()";
const SUBPROCESS_REMEDIATION: &str = "express side effects through \
    context.orders or context.scheduler instead of a subprocess";

/// The Python rule table, in code order.
const RULES: &[RuleSpec] = &[
    RuleSpec {
        code: "SB001",
        category: ViolationCategory::WallClock,
        module: "time",
        attributes: Some(TIME_WALL_CLOCK),
        summary: "reads the wall clock, so replay cannot reproduce the same decision",
        remediation: "read engine time from context.clock, for example context.clock.now()",
    },
    RuleSpec {
        code: "SB002",
        category: ViolationCategory::WallClock,
        module: "datetime",
        attributes: Some(DATETIME_WALL_CLOCK),
        summary: "reads the current date or time, so replay cannot reproduce the same decision",
        remediation: "build timestamps explicitly, for example datetime(2024, 1, 2, tzinfo=UTC), \
            or read context.clock",
    },
    RuleSpec {
        code: "SB003",
        category: ViolationCategory::WallClock,
        module: "os",
        attributes: Some(OS_WALL_CLOCK),
        summary: "reads process CPU or wall time outside the injected clock",
        remediation: "use context.clock for every timestamp a strategy observes",
    },
    RuleSpec {
        code: "SB100",
        category: ViolationCategory::Sleeping,
        module: "time",
        attributes: Some(TIME_SLEEP),
        summary: "sleeps inside the strategy and stalls the deterministic event loop",
        remediation: "schedule a timestamped event through context.scheduler instead of sleeping",
    },
    RuleSpec {
        code: "SB200",
        category: ViolationCategory::Subprocess,
        module: "subprocess",
        attributes: None,
        summary: "starts another process, which the kernel cannot order or replay",
        remediation: SUBPROCESS_REMEDIATION,
    },
    RuleSpec {
        code: "SB201",
        category: ViolationCategory::Subprocess,
        module: "pty",
        attributes: None,
        summary: "spawns another process through a pseudo-terminal",
        remediation: SUBPROCESS_REMEDIATION,
    },
    RuleSpec {
        code: "SB202",
        category: ViolationCategory::Subprocess,
        module: "os",
        attributes: Some(OS_SUBPROCESS),
        summary: "starts another process, which the kernel cannot order or replay",
        remediation: SUBPROCESS_REMEDIATION,
    },
    RuleSpec {
        code: "SB300",
        category: ViolationCategory::Networking,
        module: "aiohttp",
        attributes: None,
        summary: "performs network I/O through the aiohttp module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB301",
        category: ViolationCategory::Networking,
        module: "ftplib",
        attributes: None,
        summary: "performs network I/O through the ftplib module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB302",
        category: ViolationCategory::Networking,
        module: "http",
        attributes: None,
        summary: "performs network I/O through the http module, which the kernel cannot order or \
            replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB303",
        category: ViolationCategory::Networking,
        module: "httpx",
        attributes: None,
        summary: "performs network I/O through the httpx module, which the kernel cannot order or \
            replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB304",
        category: ViolationCategory::Networking,
        module: "imaplib",
        attributes: None,
        summary: "performs network I/O through the imaplib module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB305",
        category: ViolationCategory::Networking,
        module: "nntplib",
        attributes: None,
        summary: "performs network I/O through the nntplib module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB306",
        category: ViolationCategory::Networking,
        module: "poplib",
        attributes: None,
        summary: "performs network I/O through the poplib module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB307",
        category: ViolationCategory::Networking,
        module: "requests",
        attributes: None,
        summary: "performs network I/O through the requests module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB308",
        category: ViolationCategory::Networking,
        module: "smtplib",
        attributes: None,
        summary: "performs network I/O through the smtplib module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB309",
        category: ViolationCategory::Networking,
        module: "socket",
        attributes: None,
        summary: "performs network I/O through the socket module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB310",
        category: ViolationCategory::Networking,
        module: "socketserver",
        attributes: None,
        summary: "performs network I/O through the socketserver module, which the kernel cannot \
            order or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB311",
        category: ViolationCategory::Networking,
        module: "ssl",
        attributes: None,
        summary: "performs network I/O through the ssl module, which the kernel cannot order or \
            replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB312",
        category: ViolationCategory::Networking,
        module: "telnetlib",
        attributes: None,
        summary:
            "performs network I/O through the telnetlib module, which the kernel cannot order \
            or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB313",
        category: ViolationCategory::Networking,
        module: "urllib",
        attributes: None,
        summary:
            "performs network I/O through the urllib module, which the kernel cannot order or \
            replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB314",
        category: ViolationCategory::Networking,
        module: "websockets",
        attributes: None,
        summary: "performs network I/O through the websockets module, which the kernel cannot \
            order or replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB315",
        category: ViolationCategory::Networking,
        module: "xmlrpc",
        attributes: None,
        summary:
            "performs network I/O through the xmlrpc module, which the kernel cannot order or \
            replay",
        remediation: NETWORK_REMEDIATION,
    },
    RuleSpec {
        code: "SB400",
        category: ViolationCategory::UnseededRandom,
        module: "random",
        attributes: Some(RANDOM_UNSEEDED),
        summary: "draws process-global randomness that replay cannot reproduce",
        remediation: RANDOM_REMEDIATION,
    },
    RuleSpec {
        code: "SB401",
        category: ViolationCategory::UnseededRandom,
        module: "secrets",
        attributes: None,
        summary: "draws cryptographic entropy that replay cannot reproduce",
        remediation: RANDOM_REMEDIATION,
    },
    RuleSpec {
        code: "SB402",
        category: ViolationCategory::UnseededRandom,
        module: "os",
        attributes: Some(OS_ENTROPY),
        summary: "draws operating-system entropy that replay cannot reproduce",
        remediation: RANDOM_REMEDIATION,
    },
    RuleSpec {
        code: "SB403",
        category: ViolationCategory::UnseededRandom,
        module: "uuid",
        attributes: Some(UUID_ENTROPY),
        summary: "derives an identifier from the clock or from entropy",
        remediation: "derive identifiers deterministically from context.state or from an event \
            identifier",
    },
    RuleSpec {
        code: "SB500",
        category: ViolationCategory::Asyncio,
        module: "anyio",
        attributes: None,
        summary: "drives an asynchronous event loop (anyio) inside the synchronous kernel",
        remediation: ASYNC_REMEDIATION,
    },
    RuleSpec {
        code: "SB501",
        category: ViolationCategory::Asyncio,
        module: "asyncio",
        attributes: None,
        summary: "drives an asynchronous event loop (asyncio) inside the synchronous kernel",
        remediation: ASYNC_REMEDIATION,
    },
    RuleSpec {
        code: "SB502",
        category: ViolationCategory::Asyncio,
        module: "curio",
        attributes: None,
        summary: "drives an asynchronous event loop (curio) inside the synchronous kernel",
        remediation: ASYNC_REMEDIATION,
    },
    RuleSpec {
        code: "SB503",
        category: ViolationCategory::Asyncio,
        module: "trio",
        attributes: None,
        summary: "drives an asynchronous event loop (trio) inside the synchronous kernel",
        remediation: ASYNC_REMEDIATION,
    },
    RuleSpec {
        code: "SB600",
        category: ViolationCategory::Concurrency,
        module: "_thread",
        attributes: None,
        summary: "runs work concurrently through _thread, whose ordering the kernel cannot replay",
        remediation: CONCURRENCY_REMEDIATION,
    },
    RuleSpec {
        code: "SB601",
        category: ViolationCategory::Concurrency,
        module: "concurrent",
        attributes: None,
        summary: "runs work concurrently through concurrent, whose ordering the kernel cannot \
            replay",
        remediation: CONCURRENCY_REMEDIATION,
    },
    RuleSpec {
        code: "SB602",
        category: ViolationCategory::Concurrency,
        module: "eventlet",
        attributes: None,
        summary: "runs work concurrently through eventlet, whose ordering the kernel cannot replay",
        remediation: CONCURRENCY_REMEDIATION,
    },
    RuleSpec {
        code: "SB603",
        category: ViolationCategory::Concurrency,
        module: "gevent",
        attributes: None,
        summary: "runs work concurrently through gevent, whose ordering the kernel cannot replay",
        remediation: CONCURRENCY_REMEDIATION,
    },
    RuleSpec {
        code: "SB604",
        category: ViolationCategory::Concurrency,
        module: "multiprocessing",
        attributes: None,
        summary:
            "runs work concurrently through multiprocessing, whose ordering the kernel cannot \
            replay",
        remediation: CONCURRENCY_REMEDIATION,
    },
    RuleSpec {
        code: "SB605",
        category: ViolationCategory::Concurrency,
        module: "threading",
        attributes: None,
        summary: "runs work concurrently through threading, whose ordering the kernel cannot \
            replay",
        remediation: CONCURRENCY_REMEDIATION,
    },
];

/// The async-syntax rule reported for `async def`, `await`, and async loops.
const ASYNC_SYNTAX_RULE: RuleSpec = RuleSpec {
    code: "SB700",
    category: ViolationCategory::Asyncio,
    module: "",
    attributes: None,
    summary: "uses async syntax, which cannot run inside the synchronous kernel",
    remediation: "implement callbacks synchronously and schedule timestamped events through \
        context.scheduler",
};

/// The whole-module rule for a root module name, if any.
fn whole_module_rule(root: &str) -> Option<&'static RuleSpec> {
    RULES
        .iter()
        .rev()
        .find(|rule| rule.attributes.is_none() && rule.module == root)
}

/// The attribute rule for a root module and attribute name, if any.
fn attribute_rule(root: &str, attribute: &str) -> Option<&'static RuleSpec> {
    RULES.iter().rev().find(|rule| {
        rule.module == root
            && rule
                .attributes
                .is_some_and(|attributes| attributes.contains(&attribute))
    })
}

/// Return the dotted name of a pure name/attribute chain.
///
/// A chain that passes through a call result is intentionally not a dotted
/// module path: `random.Random(7).random()` calls a method on a returned object
/// rather than on the `random` module.
fn dotted_parts(expression: &ast::Expr) -> Option<Vec<String>> {
    match expression {
        ast::Expr::Name(name) => Some(vec![name.id.to_string()]),
        ast::Expr::Attribute(attribute) => {
            let mut parts = dotted_parts(&attribute.value)?;
            parts.push(attribute.attr.to_string());
            Some(parts)
        }
        _ => None,
    }
}

/// Translate a byte offset into the Python convention `(lineno, col_offset)`.
///
/// `lineno` is one-based and `col_offset` is a zero-based UTF-8 byte offset,
/// matching `ast` node positions.
fn locate(source: &str, offset: TextRange) -> (i64, i64) {
    let offset = offset.start().to_u32() as usize;
    let bytes = source.as_bytes();
    let end = offset.min(bytes.len());
    let mut lineno = 1_i64;
    let mut line_start = 0_usize;
    for (index, byte) in bytes[..end].iter().enumerate() {
        if *byte == b'\n' {
            lineno += 1;
            line_start = index + 1;
        }
    }
    (lineno, (end - line_start) as i64)
}

/// The AST walker collecting sandbox violations from one parsed strategy.
struct SandboxVisitor<'a> {
    source: &'a str,
    filename: &'a str,
    violations: Vec<SandboxViolation>,
    modules: HashMap<String, String>,
    reported_modules: HashSet<String>,
    checked: HashSet<TextRange>,
}

impl<'a> SandboxVisitor<'a> {
    fn new(source: &'a str, filename: &'a str) -> Self {
        Self {
            source,
            filename,
            violations: Vec::new(),
            modules: HashMap::new(),
            reported_modules: HashSet::new(),
            checked: HashSet::new(),
        }
    }

    fn into_report(self) -> LintReport {
        let mut violations = self.violations;
        violations.sort_by(|left, right| {
            (left.lineno, left.col_offset, &left.code).cmp(&(
                right.lineno,
                right.col_offset,
                &right.code,
            ))
        });
        LintReport {
            filename: self.filename.to_string(),
            violations,
        }
    }

    fn record(&mut self, rule: &'static RuleSpec, anchor: TextRange, symbol: &str) {
        let (lineno, col_offset) = locate(self.source, anchor);
        let summary = rule.summary.replace("{module}", rule.module);
        self.violations.push(SandboxViolation {
            filename: self.filename.to_string(),
            lineno,
            col_offset,
            code: rule.code.to_string(),
            category: rule.category,
            symbol: symbol.to_string(),
            message: format!("{symbol}: {summary}"),
            remediation: rule.remediation.to_string(),
        });
    }

    fn check_expr(&mut self, expression: &ast::Expr, anchor: TextRange) {
        let key = expression.range();
        if !self.checked.insert(key) {
            return;
        }
        let Some(parts) = dotted_parts(expression) else {
            return;
        };
        self.resolve_reference(&parts, anchor);
    }

    fn check_attribute(&mut self, node: &ast::ExprAttribute, anchor: TextRange) {
        if !self.checked.insert(node.range()) {
            return;
        }
        let Some(mut parts) = dotted_parts(&node.value) else {
            return;
        };
        parts.push(node.attr.to_string());
        self.resolve_reference(&parts, anchor);
    }

    fn resolve_reference(&mut self, parts: &[String], anchor: TextRange) {
        let Some(name) = parts.first() else {
            return;
        };
        let chain = &parts[1..];
        if chain.is_empty() {
            return;
        }
        let Some(module) = self.modules.get(name) else {
            return;
        };
        let root = module.split('.').next().unwrap_or("");
        if self.reported_modules.contains(root) {
            return;
        }
        let attribute = chain[chain.len() - 1].as_str();
        if let Some(rule) = attribute_rule(root, attribute) {
            let symbol = std::iter::once(module.as_str())
                .chain(chain.iter().map(String::as_str))
                .collect::<Vec<_>>()
                .join(".");
            self.record(rule, anchor, &symbol);
        }
    }
}

impl Visitor for SandboxVisitor<'_> {
    fn visit_stmt_import(&mut self, node: ast::StmtImport) {
        let anchor = node.range();
        for alias in &node.names {
            let module = alias.name.to_string();
            let root = module.split('.').next().unwrap_or("").to_string();
            let key = alias
                .asname
                .as_ref()
                .map_or_else(|| root.clone(), ToString::to_string);
            self.modules.insert(key, module.clone());
            if let Some(rule) = whole_module_rule(&root) {
                if self.reported_modules.insert(root) {
                    self.record(rule, anchor, &module);
                }
            }
        }
    }

    fn visit_stmt_import_from(&mut self, node: ast::StmtImportFrom) {
        if node.level.is_some_and(|level| level.to_u32() > 0) {
            return;
        }
        let anchor = node.range();
        let module = node
            .module
            .as_ref()
            .map_or_else(String::new, ToString::to_string);
        let root = module.split('.').next().unwrap_or("").to_string();
        for alias in &node.names {
            let name = alias.name.to_string();
            let bound = alias
                .asname
                .as_ref()
                .map_or_else(|| name.clone(), ToString::to_string);
            let mut rule = if root.is_empty() {
                None
            } else {
                attribute_rule(&root, &name)
            };
            if rule.is_none() && !root.is_empty() {
                rule = whole_module_rule(&root);
            }
            match rule {
                Some(rule) => {
                    let symbol = if module.is_empty() {
                        name
                    } else {
                        format!("{module}.{name}")
                    };
                    self.record(rule, anchor, &symbol);
                }
                None => {
                    if !module.is_empty() {
                        self.modules.insert(bound, format!("{module}.{name}"));
                    }
                }
            }
        }
    }

    fn visit_stmt_async_function_def(&mut self, node: ast::StmtAsyncFunctionDef) {
        self.record(&ASYNC_SYNTAX_RULE, node.range(), "async def");
        self.generic_visit_stmt_async_function_def(node);
    }

    fn visit_stmt_async_for(&mut self, node: ast::StmtAsyncFor) {
        self.record(&ASYNC_SYNTAX_RULE, node.range(), "async for");
        self.generic_visit_stmt_async_for(node);
    }

    fn visit_stmt_async_with(&mut self, node: ast::StmtAsyncWith) {
        self.record(&ASYNC_SYNTAX_RULE, node.range(), "async with");
        self.generic_visit_stmt_async_with(node);
    }

    fn visit_expr_await(&mut self, node: ast::ExprAwait) {
        self.record(&ASYNC_SYNTAX_RULE, node.range(), "await");
        self.generic_visit_expr_await(node);
    }

    fn visit_expr_call(&mut self, node: ast::ExprCall) {
        self.check_expr(&node.func, node.range());
        self.generic_visit_expr_call(node);
    }

    fn visit_expr_attribute(&mut self, node: ast::ExprAttribute) {
        self.check_attribute(&node, node.range());
        self.generic_visit_expr_attribute(node);
    }

    fn visit_comprehension(&mut self, node: ast::Comprehension) {
        if node.is_async {
            self.record(&ASYNC_SYNTAX_RULE, node.iter.range(), "async for");
        }
        self.generic_visit_comprehension(node);
    }
}

/// The native strategy sandbox linter.
///
/// The linter is stateless; it parses text with `rustpython-parser` and never
/// imports or executes strategy code.
#[derive(Debug, Clone, Copy, Default)]
pub struct StrategyLinter;

impl StrategyLinter {
    /// Compose a native linter.
    #[must_use]
    pub fn new() -> Self {
        Self
    }

    /// Lint strategy source text and return every sandbox violation.
    ///
    /// # Errors
    ///
    /// Returns [`StrategyLintError`] when the source is not valid Python.
    pub fn lint_source(
        &self,
        source: &str,
        filename: Option<&str>,
    ) -> Result<LintReport, StrategyLintError> {
        let filename = filename.unwrap_or(DEFAULT_FILENAME);
        let suite = ast::Suite::parse(source, filename).map_err(|error| {
            StrategyLintError(format!("{filename} is not valid Python: {error}"))
        })?;
        let mut visitor = SandboxVisitor::new(source, filename);
        for statement in suite {
            visitor.visit_stmt(statement);
        }
        Ok(visitor.into_report())
    }

    /// Lint a strategy file and return every sandbox violation.
    ///
    /// # Errors
    ///
    /// Returns [`StrategyLintError`] when the file cannot be read or parsed.
    pub fn lint_file(&self, path: &Path) -> Result<LintReport, StrategyLintError> {
        let source =
            std::fs::read_to_string(path).map_err(|error| StrategyLintError(error.to_string()))?;
        self.lint_source(&source, Some(&path.to_string_lossy()))
    }

    /// Lint every Python file under a directory and aggregate the violations.
    ///
    /// # Errors
    ///
    /// Returns [`StrategyLintError`] when `root` is not a directory or a file
    /// cannot be read or parsed.
    pub fn lint_directory(&self, root: &Path) -> Result<LintReport, StrategyLintError> {
        if !root.is_dir() {
            return Err(StrategyLintError(format!(
                "{} is not a directory",
                root.display()
            )));
        }
        let mut files = Vec::new();
        collect_python_files(root, &mut files)?;
        files.sort();
        let mut violations = Vec::new();
        for path in files {
            violations.extend(self.lint_file(&path)?.violations);
        }
        violations.sort_by(|left, right| {
            (&left.filename, left.lineno, left.col_offset, &left.code).cmp(&(
                &right.filename,
                right.lineno,
                right.col_offset,
                &right.code,
            ))
        });
        Ok(LintReport {
            filename: root.to_string_lossy().to_string(),
            violations,
        })
    }

    /// Lint source and return an error when it is not clean.
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

/// Recursively collect `*.py` files, skipping `__pycache__` and dotfiles.
fn collect_python_files(root: &Path, files: &mut Vec<PathBuf>) -> Result<(), StrategyLintError> {
    let entries = std::fs::read_dir(root).map_err(|error| StrategyLintError(error.to_string()))?;
    for entry in entries {
        let entry = entry.map_err(|error| StrategyLintError(error.to_string()))?;
        let path = entry.path();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if path.is_dir() {
            if name != "__pycache__" && !name.starts_with('.') {
                collect_python_files(&path, files)?;
            }
            continue;
        }
        if path.extension().is_some_and(|extension| extension == "py") && !name.starts_with('.') {
            files.push(path);
        }
    }
    Ok(())
}

#[cfg(test)]
#[path = "../tests/unit/lint.rs"]
mod tests;
