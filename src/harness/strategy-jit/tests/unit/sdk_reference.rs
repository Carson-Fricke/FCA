use super::source_digest;

#[test]
fn sdk_source_digest_is_portable_and_preserves_real_source_changes() {
    // Shared UTF-8 golden with tests/thesis/workspaces/test_sdk.py.
    let source = "// café\npub const VALUE: u32 = 7;\n";
    let expected = "1082bf8ebecd95e5b8b748ba79f12aa232bc8364fe2d4d3d50081b550104a0d4";
    for text in [
        source.to_owned(),
        source.replace('\n', "\r\n"),
        source.replacen('\n', "\r\n", 1),
    ] {
        assert_eq!(source_digest(text.as_bytes()), expected);
    }
    for changed in [
        source.replace("7", "8"),
        source.replace(" = ", "  = "),
        source.trim_end().to_owned(),
        source.replace("café", "cafe"),
        source.replace('\n', "\r"),
    ] {
        assert_ne!(source_digest(changed.as_bytes()), expected);
    }
}
