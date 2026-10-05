use super::*;

use crate::compile::fixture::Fixture;

thread_local! {
    static PROBES: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

pub(super) fn record_probe() {
    PROBES.with(|probes| probes.set(probes.get() + 1));
}

#[test]
fn rustup_settings_changes_invalidate_which_and_version_probes() {
    let fixture = Fixture::new();
    let settings = fixture.root.join("rustup-settings.toml");
    let probe = |tool: &str, args: &[&str]| {
        compiler_probe_with_settings(tool.as_ref(), args, fixture.workspace.root(), &settings)
            .unwrap()
    };
    std::fs::write(&settings, "default_toolchain = 'stable'\n[overrides]\n").unwrap();
    let first = probe("rustup", &["which", "rustc"]);
    let version = probe("rustc", &["--version", "--verbose"]);
    let count = PROBES.with(|probes| probes.get());
    assert_eq!(probe("rustup", &["which", "rustc"]), first);
    assert_eq!(probe("rustc", &["--version", "--verbose"]), version);
    assert_eq!(PROBES.with(|probes| probes.get()), count);
    std::fs::write(
        &settings,
        "default_toolchain = 'stable'\n[overrides]\n'/fixture/workspace' = 'nightly'\n",
    )
    .unwrap();
    probe("rustup", &["which", "rustc"]);
    let changed_which = PROBES.with(|probes| probes.get());
    assert_eq!(changed_which, count + 1);
    probe("rustc", &["--version", "--verbose"]);
    assert!(PROBES.with(|probes| probes.get()) > changed_which);
}

#[test]
fn compiler_fingerprint_tracks_configuration_and_toolchain_and_fails_closed() {
    let fixture = Fixture::new();
    let fingerprint =
        || compiler_fingerprint(fixture.workspace.root(), &fixture.sdk, "toolchain").unwrap();
    let original = fingerprint();
    assert_eq!(fingerprint(), original);
    assert_ne!(
        compiler_fingerprint(fixture.workspace.root(), &fixture.sdk, "changed-toolchain").unwrap(),
        original
    );
    let cargo = fixture.workspace.root().join(".cargo");
    std::fs::create_dir_all(&cargo).unwrap();
    std::fs::write(cargo.join("config.toml"), "[build]\njobs = 1\n").unwrap();
    let configured = fingerprint();
    assert_ne!(configured, original);
    std::fs::write(cargo.join("config.toml"), "[build]\njobs = 2\n").unwrap();
    assert_ne!(fingerprint(), configured);
    std::fs::remove_file(cargo.join("config.toml")).unwrap();
    assert_eq!(fingerprint(), original);
    std::fs::write(fixture.sdk.join("build.rs"), "fn main() {}\n").unwrap();
    assert_ne!(fingerprint(), original);
    std::fs::remove_file(fixture.sdk.join("Cargo.toml")).unwrap();
    assert!(compiler_fingerprint(fixture.workspace.root(), &fixture.sdk, "toolchain").is_err());
}

#[test]
fn validation_identity_is_exactly_the_shared_compiler_fingerprint() {
    let workspace = crate::workspace::ManagedWorkspace::open_default().unwrap();
    let toolchain = crate::toolchain::resolve_toolchain(workspace.root()).unwrap();
    let fingerprint = compiler_fingerprint(
        workspace.root(),
        Path::new(&crate::compiler::sdk_dependency_path()),
        &toolchain.digest,
    )
    .unwrap();
    assert_eq!(
        crate::validation::validation_service()
            .validation_identity()
            .unwrap(),
        format!("sha256:{fingerprint}")
    );
}
