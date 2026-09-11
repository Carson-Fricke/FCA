use std::fs;
use std::sync::atomic::{AtomicUsize, Ordering};

use super::*;

static COUNTER: AtomicUsize = AtomicUsize::new(0);

const WALL_CLOCK_SOURCE: &str = "import time\nfrom datetime import datetime\n\
        \nvalue = time.time()\nstamp = datetime.now()\n";

const SLEEPING_SOURCE: &str = "import time\n\ndef on_start(context):\n    time.sleep(0.1)\n";

const FROM_IMPORT_SLEEP_SOURCE: &str =
    "from time import sleep\n\ndef on_start(context):\n    sleep(0.1)\n";

const NETWORK_SOURCE: &str = "import socket\n\nconnection = socket.socket()\n";

const NETWORK_FROM_IMPORT_SOURCE: &str =
    "from httpx import get\n\nresponse = get(\"https://example.invalid\")\n";

const SUBPROCESS_SOURCE: &str =
    "import subprocess\n\nsubprocess.run([\"echo\", \"started\"], check=True)\n";

const SUBPROCESS_OS_SOURCE: &str = "import os\n\nos.system(\"echo started\")\n";

const UNSEEDED_RANDOM_SOURCE: &str = "import os\nimport random\nimport secrets\nimport uuid\n\
        \n_draw = random.random()\n_size = random.randint(1, 10)\n_system = random.SystemRandom()\n\
        _token = secrets.token_hex(8)\n_salt = os.urandom(4)\n_tag = uuid.uuid4()\n";

const ASYNC_SOURCE: &str =
    "import asyncio\n\nasync def on_start(context):\n    await asyncio.sleep(0.1)\n";

const ASYNC_COMPREHENSION_SOURCE: &str =
    "def on_start(context):\n    values = [value async for value in context.stream()]\n";

const BARE_ATTRIBUTE_SOURCE: &str = "import datetime\n\nnow = datetime.datetime.now\n";

const CONCURRENCY_SOURCE: &str =
    "import threading\nfrom concurrent.futures import ThreadPoolExecutor\n\
        \n_worker = threading.Thread(target=poll)\n_pool = ThreadPoolExecutor(max_workers=1)\n";

const CONTEXT_BOUNDARY_SOURCE: &str = concat!(
    "from harness.strategy.contracts import Strategy\n",
    "\n",
    "class ContextBoundaryStrategy(Strategy):\n",
    "    def on_start(self, context):\n",
    "        now = context.clock.now()\n",
    "        context.random.random()\n",
    "        context.random.randint(1, 10)\n",
    "        context.data.read(now, \"AAPL\")\n",
);

const SEEDED_RANDOM_SOURCE: &str =
    "from random import Random\n\ngenerator = Random(1729)\ndraw = generator.random()\n";

fn lint(source: &str) -> LintReport {
    StrategyLinter::new()
        .lint_source(source, Some("<test>"))
        .expect("lint")
}

fn temp_dir(tag: &str) -> PathBuf {
    let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!(
        "harness-host-lint-{}-{tag}-{unique}",
        std::process::id()
    ));
    fs::create_dir_all(&dir).expect("create temp dir");
    dir
}

#[test]
fn clean_sources_produce_no_violations() {
    let report = lint("def on_quote(event):\n    return event\n");
    assert!(report.ok(), "{}", report.render());
    assert!(report.render().contains("clean"));
}

#[test]
fn context_boundaries_are_allowed() {
    let report = lint(CONTEXT_BOUNDARY_SOURCE);
    assert!(report.ok(), "{}", report.render());
}

#[test]
fn seeded_random_is_allowed() {
    let report = lint(SEEDED_RANDOM_SOURCE);
    assert!(report.ok(), "{}", report.render());
}

#[test]
fn wall_clock_reads_are_rejected() {
    let report = lint(WALL_CLOCK_SOURCE);
    assert_eq!(
        report.categories(),
        BTreeSet::from([ViolationCategory::WallClock])
    );
    assert_eq!(report.violations.len(), 2);
    assert_eq!(report.codes(), vec!["SB001", "SB002"]);
}

#[test]
fn sleeping_is_rejected() {
    let report = lint(SLEEPING_SOURCE);
    assert_eq!(report.codes(), vec!["SB100"]);
    assert_eq!(
        report.categories(),
        BTreeSet::from([ViolationCategory::Sleeping])
    );
}

#[test]
fn prohibited_from_imports_are_rejected() {
    let report = lint(FROM_IMPORT_SLEEP_SOURCE);
    assert_eq!(report.codes(), vec!["SB100"]);
    assert_eq!(report.violations[0].symbol, "time.sleep");
}

#[test]
fn networking_module_imports_are_rejected() {
    let report = lint(NETWORK_SOURCE);
    assert_eq!(report.violations[0].symbol, "socket");
    assert_eq!(
        report.categories(),
        BTreeSet::from([ViolationCategory::Networking])
    );
}

#[test]
fn networking_from_imports_are_rejected() {
    let report = lint(NETWORK_FROM_IMPORT_SOURCE);
    assert_eq!(report.violations[0].symbol, "httpx.get");
}

#[test]
fn subprocess_modules_are_rejected() {
    let report = lint(SUBPROCESS_SOURCE);
    assert_eq!(report.violations[0].symbol, "subprocess");
    assert_eq!(
        report.categories(),
        BTreeSet::from([ViolationCategory::Subprocess])
    );
}

#[test]
fn os_process_helpers_are_rejected() {
    let report = lint(SUBPROCESS_OS_SOURCE);
    assert_eq!(report.violations[0].symbol, "os.system");
}

#[test]
fn unseeded_random_is_rejected() {
    let report = lint(UNSEEDED_RANDOM_SOURCE);
    assert_eq!(report.violations.len(), 6);
    assert_eq!(
        report.categories(),
        BTreeSet::from([ViolationCategory::UnseededRandom])
    );
}

#[test]
fn async_syntax_is_rejected() {
    let report = lint(ASYNC_SOURCE);
    assert_eq!(
        report.categories(),
        BTreeSet::from([ViolationCategory::Asyncio])
    );
    assert_eq!(
        report
            .violations
            .iter()
            .filter(|v| v.code == "SB700")
            .count(),
        2
    );
}

#[test]
fn async_comprehensions_are_rejected() {
    let report = lint(ASYNC_COMPREHENSION_SOURCE);
    let symbols: Vec<&str> = report
        .violations
        .iter()
        .map(|v| v.symbol.as_str())
        .collect();
    assert_eq!(symbols, vec!["async for"]);
    assert!(report.violations[0].lineno > 0);
}

#[test]
fn bare_wall_clock_attribute_is_rejected() {
    let report = lint(BARE_ATTRIBUTE_SOURCE);
    assert_eq!(report.codes(), vec!["SB002"]);
    assert_eq!(report.violations[0].symbol, "datetime.datetime.now");
}

#[test]
fn concurrency_is_rejected() {
    let report = lint(CONCURRENCY_SOURCE);
    assert_eq!(report.violations.len(), 2);
    assert_eq!(
        report.categories(),
        BTreeSet::from([ViolationCategory::Concurrency])
    );
}

#[test]
fn violations_point_at_the_offending_source() {
    let report = lint(SLEEPING_SOURCE);
    let expected = SLEEPING_SOURCE
        .lines()
        .position(|line| line.contains("time.sleep"))
        .map(|index| index as i64 + 1)
        .expect("source line");
    assert_eq!(report.violations[0].lineno, expected);
}

#[test]
fn violations_are_actionable() {
    for violation in lint(&format!("{UNSEEDED_RANDOM_SOURCE}\n{ASYNC_SOURCE}")).violations {
        assert!(violation.code.starts_with("SB"));
        assert_eq!(violation.filename, "<test>");
        assert!(violation.lineno > 0);
        assert!(violation.col_offset >= 0);
        assert!(violation.message.starts_with(&violation.symbol));
        assert!(violation.remediation.contains("context."));
        assert_eq!(
            violation.location(),
            format!(
                "{}:{}:{}",
                violation.filename, violation.lineno, violation.col_offset
            )
        );
        assert!(violation.format().contains(&violation.code));
    }
}

#[test]
fn unparsable_source_raises_strategy_lint_error() {
    let error = StrategyLinter::new()
        .lint_source("def broken(", Some("<test>"))
        .expect_err("error");
    assert!(error.0.contains("not valid Python"), "{error}");
}

#[test]
fn assert_clean_rejects_violations() {
    let error = StrategyLinter::new()
        .assert_clean("import time\nvalue = time.time()\n", Some("<test>"))
        .expect_err("error");
    assert!(error.0.contains("SB001"));
}

#[test]
fn lint_file_and_directory_match_source_linting() {
    let dir = temp_dir("directory");
    let clean = dir.join("clean.py");
    let sleeping = dir.join("sleeping.py");
    fs::write(&clean, "def on_quote(event):\n    return event\n").expect("write clean");
    fs::write(&sleeping, SLEEPING_SOURCE).expect("write sleeping");

    let report = StrategyLinter::new()
        .lint_directory(&dir)
        .expect("lint dir");
    assert_eq!(
        report.categories(),
        BTreeSet::from([ViolationCategory::Sleeping])
    );
    assert_eq!(report.violations.len(), 1);

    let file_report = StrategyLinter::new()
        .lint_file(&sleeping)
        .expect("lint file");
    assert_eq!(file_report.codes(), vec!["SB100"]);
    let _ = fs::remove_dir_all(dir);
}

#[test]
fn missing_directory_raises_strategy_lint_error() {
    let missing = temp_dir("missing").join("nested");
    let error = StrategyLinter::new()
        .lint_directory(&missing)
        .expect_err("error");
    assert!(error.0.contains("is not a directory"), "{error}");
}

#[test]
fn python_fixtures_match_the_ported_rules() {
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("tests")
        .join("strategy_lint")
        .join("fixtures");
    let linter = StrategyLinter::new();

    let clean = linter
        .lint_file(&fixtures.join("clean_strategy.py"))
        .expect("clean fixture");
    assert!(clean.ok(), "{}", clean.render());

    let expected = [
        ("wall_clock_strategy.py", ViolationCategory::WallClock),
        ("sleeping_strategy.py", ViolationCategory::Sleeping),
        ("networking_strategy.py", ViolationCategory::Networking),
        ("subprocess_strategy.py", ViolationCategory::Subprocess),
        (
            "unseeded_random_strategy.py",
            ViolationCategory::UnseededRandom,
        ),
        ("asyncio_strategy.py", ViolationCategory::Asyncio),
        ("concurrency_strategy.py", ViolationCategory::Concurrency),
    ];
    for (name, category) in expected {
        let report = linter
            .lint_file(&fixtures.join(name))
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        assert!(
            report.categories().contains(&category),
            "{name}: {:?}",
            report.categories()
        );
    }

    let mixed = linter
        .lint_file(&fixtures.join("mixed_violations_strategy.py"))
        .expect("mixed fixture");
    assert_eq!(mixed.violations.len(), 3);
    assert_eq!(
        mixed.categories(),
        BTreeSet::from([
            ViolationCategory::WallClock,
            ViolationCategory::Sleeping,
            ViolationCategory::Networking,
        ])
    );
}
