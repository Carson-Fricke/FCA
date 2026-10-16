use super::*;

use super::fixture::{artifact_value, replace_preserving_mtime, Fixture};

#[test]
fn distinct_authored_sources_reuse_dependencies_and_keep_loaded_artifacts_immutable() {
    let fixture = Fixture::new();
    let first = compile_with_sdk(
        &fixture.workspace,
        fixture::SOURCE,
        ProgramKind::Study,
        &fixture.sdk,
    )
    .unwrap();
    let loaded = unsafe { libloading::Library::new(&first.artifact_path).unwrap() };
    let fingerprint = fixture.workspace.read_manifest()[&first.digest]
        .compiler_fingerprint
        .clone();
    let dependency_target = fixture
        .workspace
        .target_dir()
        .join(format!("deps-{}", &fingerprint[..32]));
    let dependencies = || {
        let mut found = std::collections::BTreeMap::new();
        let mut pending = vec![dependency_target.clone()];
        while let Some(directory) = pending.pop() {
            for entry in std::fs::read_dir(directory).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    pending.push(path);
                } else if path
                    .extension()
                    .is_some_and(|extension| extension == "rlib")
                {
                    let metadata = path.metadata().unwrap();
                    found.insert(path, (metadata.modified().unwrap(), metadata.len()));
                }
            }
        }
        found
    };
    let before = dependencies();
    assert!(!before.is_empty());
    let changed_source = fixture::SOURCE.replace(
        "harness_strategy_sdk::value()",
        "harness_strategy_sdk::value() + 1",
    );
    let second = compile_with_sdk(
        &fixture.workspace,
        &changed_source,
        ProgramKind::Study,
        &fixture.sdk,
    )
    .unwrap();
    assert!(!first.cached && !second.cached);
    assert_ne!(first.artifact_path, second.artifact_path);
    assert_eq!(artifact_value(&first), 2);
    assert_eq!(artifact_value(&second), 3);
    assert_eq!(
        dependencies(),
        before,
        "unchanged dependencies must not rebuild"
    );
    std::fs::write(dependency_target.join("identity"), "different compiler").unwrap();
    let error = compile_with_sdk(
        &fixture.workspace,
        "pub fn another_source() {}",
        ProgramKind::Study,
        &fixture.sdk,
    )
    .unwrap_err();
    assert!(matches!(error, JitError::Invalid(detail) if detail.contains("identity mismatch")));
    drop(loaded);
}

#[test]
fn transitive_contract_change_rebuilds_immutable_artifact_and_workspace_key() {
    let fixture = Fixture::new();
    replace_preserving_mtime(
        &fixture.sdk.join("src/lib.rs"),
        "pub fn value() -> u32 { num::VALUE + harness_contracts::VALUE }",
    );
    let build = || {
        compile_with_sdk(
            &fixture.workspace,
            fixture::SOURCE,
            ProgramKind::Study,
            &fixture.sdk,
        )
        .unwrap()
    };
    let first = build();
    assert!(!first.cached);
    assert_eq!(artifact_value(&first), 2);
    assert!(build().cached);
    let before = fixture.workspace.read_manifest()[&first.digest]
        .compiler_fingerprint
        .clone();
    let key =
        |fingerprint: &str| digest_hex(format!("{fingerprint}:{}:study", first.digest).as_bytes());
    assert!(fixture
        .workspace
        .root()
        .join("locks")
        .join(format!("{}.lock", key(&before)))
        .is_file());

    // Same size and mtime: content, not package version or filesystem metadata, invalidates reuse.
    replace_preserving_mtime(
        &fixture.root.join("lake/contracts/src/lib.rs"),
        "pub const VALUE: u32 = 9;",
    );
    let changed = build();
    let after = fixture.workspace.read_manifest()[&changed.digest]
        .compiler_fingerprint
        .clone();
    assert_ne!(before, after);
    assert_ne!(key(&before), key(&after));
    assert!(fixture
        .workspace
        .root()
        .join("locks")
        .join(format!("{}.lock", key(&after)))
        .is_file());
    assert!(!changed.cached);
    assert_eq!(changed.digest, first.digest);
    assert_eq!(changed.sdk_version, first.sdk_version);
    assert_ne!(changed.artifact_path, first.artifact_path);
    assert_eq!(artifact_value(&changed), 10);
    assert_eq!(artifact_value(&first), 2);
    assert!(build().cached);
}

#[test]
fn relative_workspace_builds_and_preserves_authored_diagnostics() {
    let fixture = Fixture::new();
    let relative = PathBuf::from("target").join(fixture.root.file_name().unwrap());
    let workspace = ManagedWorkspace::open(&relative).unwrap();
    assert!(workspace.root().is_absolute());
    assert_eq!(
        workspace.root().canonicalize().unwrap(),
        relative.canonicalize().unwrap()
    );
    let compiled = compile_with_sdk(
        &workspace,
        fixture::SOURCE,
        ProgramKind::Study,
        &fixture.sdk,
    )
    .unwrap();
    assert_eq!(artifact_value(&compiled), 2);
    let failure = compile_with_sdk(
        &workspace,
        "this is not rust",
        ProgramKind::Study,
        &fixture.sdk,
    )
    .unwrap_err();
    match failure {
        JitError::Compile {
            diagnostics,
            detail,
            ..
        } => {
            assert!(!diagnostics.is_empty());
            assert!(detail.contains("error"));
        }
        other => panic!("expected authored diagnostics, got {other:?}"),
    }
    std::fs::remove_dir_all(workspace.root()).unwrap();
}

#[test]
fn compiler_contract_rebuilds_same_version_sources_and_legacy_manifests() {
    let fixture = Fixture::new();
    let build = || {
        compile_with_sdk(
            &fixture.workspace,
            fixture::SOURCE,
            ProgramKind::Study,
            &fixture.sdk,
        )
        .unwrap()
    };
    let first = build();
    assert!(!first.cached);
    assert_eq!(artifact_value(&first), 2);
    assert!(build().cached);
    assert_eq!(
        fixture.workspace.read_manifest()[&first.digest]
            .compiler_fingerprint
            .len(),
        64
    );
    replace_preserving_mtime(
        &fixture.sdk.join("src/lib.rs"),
        "pub fn value() -> u32 { num::VALUE + 2 }",
    );
    let sdk_changed = build();
    assert!(!sdk_changed.cached);
    assert_eq!(sdk_changed.digest, first.digest);
    assert_eq!(sdk_changed.sdk_version, first.sdk_version);
    assert_ne!(sdk_changed.artifact_path, first.artifact_path);
    assert_eq!(artifact_value(&sdk_changed), 3);
    assert_eq!(artifact_value(&first), 2);
    assert!(build().cached);
    replace_preserving_mtime(
        &fixture.root.join("harness/num/src/lib.rs"),
        "pub const VALUE: u32 = 2;",
    );
    let num_changed = build();
    assert!(!num_changed.cached);
    assert_ne!(num_changed.artifact_path, sdk_changed.artifact_path);
    assert_eq!(artifact_value(&num_changed), 4);
    let manifest = fixture
        .workspace
        .root()
        .join(crate::workspace::MANIFEST_FILE);
    let mut document: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&manifest).unwrap()).unwrap();
    document["entries"][&first.digest]
        .as_object_mut()
        .unwrap()
        .remove("compiler_fingerprint");
    std::fs::write(&manifest, serde_json::to_vec(&document).unwrap()).unwrap();
    assert!(fixture.workspace.read_manifest()[&first.digest]
        .compiler_fingerprint
        .is_empty());
    // Keep the previously returned library loaded while forcing a same-identity rebuild.
    let library = unsafe { libloading::Library::new(&num_changed.artifact_path).unwrap() };
    let legacy = build();
    assert!(!legacy.cached);
    assert_ne!(legacy.artifact_path, num_changed.artifact_path);
    assert_eq!(artifact_value(&legacy), 4);
    assert!(build().cached);
    drop(library);
}

#[test]
fn concurrent_same_input_builds_once_and_independent_inputs_retain_entries() {
    let fixture = Fixture::new();
    let barrier = std::sync::Barrier::new(4);
    let sources = [
        fixture::SOURCE.to_string(),
        format!("{}\n// independent", fixture::SOURCE),
    ];
    let results = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..4)
            .map(|index| {
                let fixture = &fixture;
                let barrier = &barrier;
                let source = &sources[index / 2];
                scope.spawn(move || {
                    let workspace = ManagedWorkspace::open(fixture.workspace.root()).unwrap();
                    barrier.wait();
                    compile_with_sdk(&workspace, source, ProgramKind::Study, &fixture.sdk).unwrap()
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect::<Vec<_>>()
    });
    for pair in results.chunks(2) {
        assert_eq!(pair.iter().filter(|artifact| !artifact.cached).count(), 1);
        assert_eq!(pair[0].artifact_path, pair[1].artifact_path);
        assert_eq!(artifact_value(&pair[0]), 2);
    }
    let entries = fixture.workspace.read_manifest();
    assert_eq!(entries.len(), 2);
    for artifact in results {
        assert!(entries.contains_key(&artifact.digest));
    }
}

#[test]
fn configured_host_target_uses_cargo_reported_artifact_path() {
    let fixture = Fixture::new();
    let host = resolve_toolchain(fixture.workspace.root()).unwrap().host;
    let config = fixture.workspace.root().join(".cargo");
    std::fs::create_dir(&config).unwrap();
    std::fs::write(
        config.join("config.toml"),
        format!("[build]\ntarget = \"{host}\"\n"),
    )
    .unwrap();
    let artifact = compile_with_sdk(
        &fixture.workspace,
        fixture::SOURCE,
        ProgramKind::Study,
        &fixture.sdk,
    )
    .unwrap();
    assert!(artifact
        .artifact_path
        .components()
        .any(|component| component.as_os_str() == host.as_str()));
    assert_eq!(artifact_value(&artifact), 2);
    assert!(
        compile_with_sdk(
            &fixture.workspace,
            fixture::SOURCE,
            ProgramKind::Study,
            &fixture.sdk
        )
        .unwrap()
        .cached
    );
}

#[test]
fn process_compile_worker() {
    let Some(root) = std::env::var_os("JIT_TEST_PROCESS_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let source = std::env::var("JIT_TEST_PROCESS_SOURCE").unwrap();
    let workspace = ManagedWorkspace::open(&root.join("managed")).unwrap();
    let artifact = compile_with_sdk(
        &workspace,
        &source,
        ProgramKind::Study,
        &root.join("harness/strategy-sdk"),
    )
    .unwrap();
    std::fs::write(root.join(std::env::var("JIT_TEST_PROCESS_RESULT").unwrap()),
        serde_json::to_vec(&serde_json::json!({"cached": artifact.cached, "path": artifact.artifact_path, "digest": artifact.digest})).unwrap()).unwrap();
}

#[test]
fn processes_build_same_input_once_and_merge_independent_inputs() {
    let fixture = Fixture::new();
    let mut children = Vec::new();
    for index in 0..4 {
        let source = if index < 2 {
            fixture::SOURCE.to_string()
        } else {
            format!("{}\n// process-independent", fixture::SOURCE)
        };
        let child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "compile::tests::process_compile_worker",
                "--nocapture",
            ])
            .env("JIT_TEST_PROCESS_ROOT", &fixture.root)
            .env("JIT_TEST_PROCESS_SOURCE", source)
            .env("JIT_TEST_PROCESS_RESULT", format!("result-{index}.json"))
            .spawn()
            .unwrap();
        children.push(child);
    }
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(90);
    for child in &mut children {
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            if std::time::Instant::now() >= deadline {
                for child in &mut children {
                    let _ = child.kill();
                    let _ = child.wait();
                }
                panic!("compile process exceeded hard timeout");
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
    }
    let results: Vec<serde_json::Value> = (0..4)
        .map(|index| {
            serde_json::from_slice(
                &std::fs::read(fixture.root.join(format!("result-{index}.json"))).unwrap(),
            )
            .unwrap()
        })
        .collect();
    for pair in results.chunks(2) {
        assert_eq!(
            pair.iter().filter(|value| value["cached"] == false).count(),
            1
        );
        assert_eq!(pair[0]["path"], pair[1]["path"]);
    }
    assert_eq!(fixture.workspace.read_manifest().len(), 2);
}
