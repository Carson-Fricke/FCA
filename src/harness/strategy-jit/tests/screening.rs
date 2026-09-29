//! The screening JIT seam: compile, load, and run authored screening
//! programs.
//!
//! Each test JIT-compiles the authored fixture (or an inline source) into the
//! managed workspace, loads the cdylib through [`LoadedScreening`], and runs
//! it over a host-built [`ScreenContext`] through
//! [`run_screening_source`], asserting rows, the host-side ranking contract,
//! the structural capability gate, and the compile cache.

use std::collections::BTreeMap;
use std::mem::size_of;
use std::path::PathBuf;

use harness_strategy_jit::{
    compile, rank_rows, run_screening_source, JitError, LoadedScreening, ManagedWorkspace,
    ProgramKind,
};
use harness_strategy_sdk::serde_json;
use harness_strategy_sdk::{
    Bar, CStr, ScreenContext, ScreenRow, ScreeningProgramVTable, SDK_ABI_VERSION, SDK_ERROR,
};

const SCREENING_SOURCE: &str = include_str!("fixtures/screening_program.rs");

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-strategy-jit-screening-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

fn bar(date: &str, close: &str) -> Bar {
    Bar {
        date: date.to_string(),
        open: "100.00".to_string(),
        high: "101.50".to_string(),
        low: "99.75".to_string(),
        close: close.to_string(),
        volume: "123456".to_string(),
    }
}

fn facts(entries: &[(&str, &str)]) -> BTreeMap<String, String> {
    entries
        .iter()
        .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
        .collect()
}

/// The as-of universe with exact-decimal facts chosen to pin ranking:
/// `BBB` (10) > `AAA` (9.5, tie with `CCC` 9.50) > `DDD` (-1) > `EEE`
/// (no momentum).
fn sample_context() -> ScreenContext {
    ScreenContext {
        as_of: "2024-01-02".to_string(),
        universe: ["AAA", "BBB", "CCC", "DDD", "EEE"]
            .iter()
            .map(|symbol| (*symbol).to_string())
            .collect(),
        bars: BTreeMap::from([
            ("AAA".to_string(), vec![bar("2024-01-02", "101.00")]),
            ("BBB".to_string(), vec![bar("2024-01-02", "99.50")]),
            ("CCC".to_string(), vec![bar("2024-01-02", "102.25")]),
            ("DDD".to_string(), vec![bar("2024-01-02", "100.00")]),
        ]),
        facts: BTreeMap::from([
            ("AAA".to_string(), facts(&[("momentum", "9.5")])),
            ("BBB".to_string(), facts(&[("momentum", "10")])),
            ("CCC".to_string(), facts(&[("momentum", "9.50")])),
            ("DDD".to_string(), facts(&[("momentum", "-1")])),
            ("EEE".to_string(), facts(&[("sector", "utilities")])),
        ]),
        indicators: BTreeMap::from([("AAA".to_string(), facts(&[("ind:sma(20)", "100.25")]))]),
    }
}

fn symbols(rows: &[ScreenRow]) -> Vec<&str> {
    rows.iter().map(|row| row.symbol.as_str()).collect()
}

#[test]
fn runs_a_jit_authored_screening_program_and_ranks_its_rows() {
    let directory = temp_dir("run");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let parameters = serde_json::json!({ "label": "fixture" });

    let rows = run_screening_source(
        &workspace,
        SCREENING_SOURCE,
        &parameters,
        &sample_context(),
        "momentum",
    )
    .expect("screening run");

    // Primary metric descending with exact decimal comparison, symbol
    // ascending on ties, rows missing the metric last.
    assert_eq!(symbols(&rows), ["BBB", "AAA", "CCC", "DDD", "EEE"]);
    assert_eq!(
        rows[0].metrics.get("momentum").map(String::as_str),
        Some("10")
    );
    // `9.5` and `9.50` are exactly equal, so the symbol tiebreak orders them.
    assert_eq!(
        rows[1].metrics.get("momentum").map(String::as_str),
        Some("9.5")
    );
    assert_eq!(
        rows[2].metrics.get("momentum").map(String::as_str),
        Some("9.50")
    );
    // The close and indicator metrics came from the host context.
    assert_eq!(
        rows[1].metrics.get("close").map(String::as_str),
        Some("101.00")
    );
    assert_eq!(
        rows[1].metrics.get("ind:sma(20)").map(String::as_str),
        Some("100.25")
    );
    // The parameter document reached the authored program.
    assert!(rows
        .iter()
        .all(|row| row.metrics.get("label").map(String::as_str) == Some("fixture")));
    // The row without the rank metric sorts last, symbol ascending.
    assert!(!rows[4].metrics.contains_key("momentum"));
    assert!(!rows[4].metrics.contains_key("close"));

    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn ranks_on_any_caller_named_exact_decimal_metric() {
    let directory = temp_dir("rank-close");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");

    let rows = run_screening_source(
        &workspace,
        SCREENING_SOURCE,
        &serde_json::json!({}),
        &sample_context(),
        "close",
    )
    .expect("screening run");

    assert_eq!(symbols(&rows), ["CCC", "AAA", "DDD", "BBB", "EEE"]);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn rank_rows_sorts_missing_and_non_decimal_metrics_last() {
    let row = |symbol: &str, metrics: &[(&str, &str)]| ScreenRow {
        symbol: symbol.to_string(),
        metrics: metrics
            .iter()
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect(),
    };
    let rows = vec![
        row("ZZZ", &[("momentum", "1")]),
        row("MMM", &[]),
        row("AAA", &[("momentum", "not-a-decimal")]),
        row("BBB", &[("momentum", "10")]),
        row("CCC", &[("momentum", "-1E-8")]),
        row("DDD", &[("momentum", "0.00000001")]),
    ];
    let ranked = rank_rows(rows, "momentum");
    // 10 > 1 > 1E-8 > -1E-8; non-decimal and missing values sort last, symbol
    // ascending.
    assert_eq!(symbols(&ranked), ["BBB", "ZZZ", "DDD", "CCC", "AAA", "MMM"]);
}

#[test]
fn a_panic_in_an_authored_screening_program_fails_closed() {
    let directory = temp_dir("panic");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let source = r#"
use harness_strategy_sdk::{export_screening_program, ScreenContext, ScreenRow, ScreeningProgram};
use harness_strategy_sdk::serde_json::Value;

struct Boom;

impl ScreeningProgram for Boom {
    fn new(_parameters: Value) -> Self { Self }
    fn screen(&mut self, _ctx: &ScreenContext) -> Vec<ScreenRow> {
        panic!("authored screening program panicked");
    }
}

export_screening_program!(Boom);
"#;
    let error = run_screening_source(
        &workspace,
        source,
        &serde_json::json!({}),
        &sample_context(),
        "momentum",
    )
    .expect_err("a panicking program must fail closed");
    let message = error.to_string();
    assert!(
        message.contains("authored screening program panicked"),
        "expected the panic detail, got {message}"
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn a_malformed_context_is_rejected_with_the_last_error() {
    let directory = temp_dir("bad-context");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let artifact =
        compile(&workspace, SCREENING_SOURCE, ProgramKind::Screening).expect("compile fixture");
    let loaded = unsafe { LoadedScreening::load(&artifact.artifact_path, &serde_json::json!({})) }
        .expect("load fixture");

    let code = unsafe { (loaded.vtable().screen)(loaded.state(), CStr::from_slice("{\"as_of\":")) };
    assert_eq!(code, SDK_ERROR);
    let message = unsafe { loaded.last_error() }.unwrap_or_default();
    assert!(
        message.contains("not valid JSON"),
        "expected the parse detail, got {message}"
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn the_screening_vtable_has_no_order_surface() {
    let directory = temp_dir("capability");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let artifact =
        compile(&workspace, SCREENING_SOURCE, ProgramKind::Screening).expect("compile fixture");
    let loaded = unsafe { LoadedScreening::load(&artifact.artifact_path, &serde_json::json!({})) }
        .expect("load fixture");

    // The vtable is the ABI header plus exactly four one-shot callbacks; there
    // is no order, market, or fill callback and no extent for one.
    assert_eq!(loaded.vtable().abi_version, SDK_ABI_VERSION);
    assert_eq!(
        size_of::<ScreeningProgramVTable>(),
        8 + 4 * size_of::<usize>()
    );
    assert!(!loaded.state().is_null());

    // The artifact exports one program kind: no study or strategy vtable
    // symbols exist, so no engine order path can reach the program.
    let library = unsafe { libloading::Library::new(&artifact.artifact_path) }.expect("reload");
    assert!(unsafe {
        library
            .get::<unsafe extern "C" fn()>(b"harness_sdk_study_vtable")
            .is_err()
    });
    assert!(unsafe {
        library
            .get::<unsafe extern "C" fn()>(b"harness_sdk_strategy_vtable")
            .is_err()
    });

    // The authoring trait cannot acquire an order surface either: a `screen`
    // that asks for an `OrderSink` does not satisfy the trait, so the
    // authored source fails to compile.
    let source = r#"
use harness_strategy_sdk::{
    export_screening_program, OrderSink, ScreenContext, ScreenRow, ScreeningProgram,
};
use harness_strategy_sdk::serde_json::Value;

struct OrderHungry;

impl ScreeningProgram for OrderHungry {
    fn new(_parameters: Value) -> Self { Self }
    fn screen(&mut self, _ctx: &ScreenContext, _orders: &mut OrderSink) -> Vec<ScreenRow> {
        Vec::new()
    }
}

export_screening_program!(OrderHungry);
"#;
    let error = compile(&workspace, source, ProgramKind::Screening)
        .expect_err("a screen with an order surface must not compile");
    assert!(
        matches!(error, JitError::Compile { .. }),
        "expected a compile failure, got {error:?}"
    );

    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn screening_compilation_is_cached_by_source_digest_and_kind() {
    let directory = temp_dir("cache");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");

    assert_eq!(
        ProgramKind::parse("screening"),
        Some(ProgramKind::Screening)
    );
    assert_eq!(ProgramKind::Screening.as_str(), "screening");
    assert_eq!(
        ProgramKind::Screening.vtable_export(),
        harness_strategy_sdk::SCREENING_VTABLE_EXPORT
    );

    let first =
        compile(&workspace, SCREENING_SOURCE, ProgramKind::Screening).expect("first compile");
    assert!(!first.cached);
    assert!(first.artifact_path.is_file());
    let second =
        compile(&workspace, SCREENING_SOURCE, ProgramKind::Screening).expect("second compile");
    assert!(
        second.cached,
        "the second screening compile must reuse the artifact"
    );
    assert_eq!(first.digest, second.digest);

    // The cache key includes the program kind: the same source compiled as a
    // study must not be served for a screening request, and vice versa.
    let as_study =
        compile(&workspace, SCREENING_SOURCE, ProgramKind::Study).expect("study compile");
    assert!(!as_study.cached);
    assert_eq!(as_study.digest, first.digest);
    let after_kind_switch =
        compile(&workspace, SCREENING_SOURCE, ProgramKind::Screening).expect("screening again");
    assert!(
        !after_kind_switch.cached,
        "a study artifact must not satisfy a screening request"
    );

    let entries = workspace.read_manifest();
    assert_eq!(
        entries.get(&first.digest).map(|entry| entry.kind.as_str()),
        Some("screening")
    );
    let _ = std::fs::remove_dir_all(&directory);
}
