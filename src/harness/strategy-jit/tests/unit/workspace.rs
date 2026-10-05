use super::*;
#[cfg(windows)]
use std::path::{Component, Prefix};

#[test]
fn compile_locks_are_scoped_and_manifest_updates_retain_all_entries() {
    let directory = std::env::temp_dir().join(format!(
        "jit-manifest-locks-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let workspace = ManagedWorkspace::open(&directory).unwrap();
    let held = workspace.lock("compiler-source-one").unwrap();
    let (sender, receiver) = std::sync::mpsc::channel();
    let root = workspace.root().to_path_buf();
    let thread = std::thread::spawn(move || {
        let other = ManagedWorkspace::open(&root).unwrap();
        let _independent = other.lock("compiler-source-two").unwrap();
        sender.send(()).unwrap();
    });
    let independent = receiver.recv_timeout(std::time::Duration::from_secs(5));
    drop(held);
    thread.join().unwrap();
    independent.expect("independent compiler/source must not block");
    std::thread::scope(|scope| {
        for index in 0..16 {
            let workspace = &workspace;
            scope.spawn(move || {
                workspace
                    .update_manifest(
                        index.to_string(),
                        WorkspaceManifest {
                            kind: "study".into(),
                            crate_name: index.to_string(),
                            artifact_relpath: "release/test.dll".into(),
                            toolchain_digest: "toolchain".into(),
                            compiler_fingerprint: "compiler".into(),
                            sdk_version: "version".into(),
                            sdk_abi_version: 1,
                            built_at_unix: 0,
                        },
                    )
                    .unwrap()
            });
        }
    });
    assert_eq!(workspace.read_manifest().len(), 16);
    std::fs::remove_dir_all(workspace.root()).unwrap();
}

#[test]
#[cfg(windows)]
fn windows_workspace_root_is_absolute_canonical_and_not_verbatim() {
    let directory = std::env::temp_dir().join(format!(
        "jit-windows-root-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
    ));
    let workspace = ManagedWorkspace::open(&directory).unwrap();
    assert!(workspace.root().is_absolute());
    assert!(
        matches!(workspace.root().components().next(), Some(Component::Prefix(prefix))
        if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::UNC(_, _)))
    );
    assert_eq!(
        workspace.root().canonicalize().unwrap(),
        directory.canonicalize().unwrap()
    );
    std::fs::remove_dir_all(workspace.root()).unwrap();
}

#[test]
#[cfg(windows)]
fn windows_prefix_conversion_only_handles_verbatim_disk_and_unc() {
    for (input, expected) in [
        (r"\\?\C:\workspace\nested", r"C:\workspace\nested"),
        (
            r"\\?\UNC\server\share\workspace",
            r"\\server\share\workspace",
        ),
        (
            r"\\?\Volume{unexpected}\workspace",
            r"\\?\Volume{unexpected}\workspace",
        ),
        (r"\\.\device", r"\\.\device"),
    ] {
        assert_eq!(
            compiler_compatible_path(PathBuf::from(input)),
            PathBuf::from(expected)
        );
    }
}
