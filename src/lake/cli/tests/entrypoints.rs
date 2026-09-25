//! Entrypoint parsing, dispatch, help, and top-level API composition.

mod support;

use std::collections::BTreeMap;

use lake_cli::{build_api_router, execute, run};
use lake_local::LocalLayout;
use support::{arguments, TempDir};

fn empty() -> BTreeMap<String, String> {
    BTreeMap::new()
}

#[test]
fn help_lists_the_top_level_and_local_command_names() {
    let top = execute(&arguments(&["--help"]), &empty());
    assert_eq!(top.code, 0, "stderr: {}", top.stderr);
    assert!(top.stderr.is_empty());
    assert!(top.stdout.contains("api"), "stdout: {}", top.stdout);
    assert!(top.stdout.contains("local"), "stdout: {}", top.stdout);

    let local = execute(&arguments(&["local", "--help"]), &empty());
    assert_eq!(local.code, 0, "stderr: {}", local.stderr);
    assert!(local.stderr.is_empty());
    for name in ["api", "ingest", "reset"] {
        assert!(local.stdout.contains(name), "stdout: {}", local.stdout);
    }
}

#[test]
fn every_subcommand_help_parses_and_dispatches() {
    for argv in [
        vec!["api", "--help"],
        vec!["local", "api", "--help"],
        vec!["local", "ingest", "--help"],
        vec!["local", "reset", "--help"],
    ] {
        let outcome = execute(&arguments(&argv), &empty());
        assert_eq!(outcome.code, 0, "argv {argv:?} stderr: {}", outcome.stderr);
        assert!(!outcome.stdout.is_empty(), "argv {argv:?}");
        assert!(outcome.stderr.is_empty(), "argv {argv:?}");
    }
}

#[test]
fn run_tolerates_the_binary_name_and_reports_exit_codes() {
    let temporary = TempDir::new("run-dispatch");
    let refused = temporary.path().join("refused");
    LocalLayout::new(&refused).initialize().expect("initialize");
    let refused_text = refused.to_string_lossy().into_owned();

    assert_eq!(run(arguments(&["--help"])), 0);
    assert_eq!(run(arguments(&["lake", "--help"])), 0);
    assert_eq!(run(arguments(&["api", "--port", "0"])), 2);
    assert_eq!(
        run(arguments(&["local", "reset", "--data-dir", &refused_text])),
        2
    );
    assert!(refused.exists());
    assert_eq!(
        run(arguments(&[
            "local",
            "reset",
            "--yes",
            "--data-dir",
            &refused_text
        ])),
        0
    );
    assert!(!refused.exists());
    assert_eq!(run(arguments(&["bogus"])), 2);
}

#[test]
fn unknown_subcommands_are_parse_failures() {
    for argv in [vec!["ingest"], vec!["operations"], vec!["bogus"]] {
        let outcome = execute(&arguments(&argv), &empty());
        assert_eq!(outcome.code, 2, "argv {argv:?}");
        assert!(outcome.stdout.is_empty(), "argv {argv:?}");
        assert!(!outcome.stderr.is_empty(), "argv {argv:?}");
    }
}

#[test]
fn ports_outside_the_range_are_parse_failures() {
    for argv in [
        vec!["api", "--port", "0"],
        vec!["api", "--port", "65536"],
        vec!["local", "api", "--port", "0"],
        vec!["local", "api", "--port", "70000"],
    ] {
        let outcome = execute(&arguments(&argv), &empty());
        assert_eq!(outcome.code, 2, "argv {argv:?}");
        assert!(
            outcome.stderr.contains("port"),
            "argv {argv:?}: {}",
            outcome.stderr
        );
    }
}

#[test]
fn top_level_api_builds_an_in_memory_router() {
    let router = build_api_router(&empty()).expect("in-memory router builds");
    drop(router);
}
