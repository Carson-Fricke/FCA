use super::*;

#[test]
fn joins_the_artifact_file_name() {
    let path = artifact_path("runs/abc", Artifact::Equity);
    assert_eq!(path, PathBuf::from("runs/abc/equity.parquet"));
}
