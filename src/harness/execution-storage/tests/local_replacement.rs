//! Real local publication preserves open readers, conditional writers and errors.
use harness_execution::persistence::Backend;
use harness_execution_storage::Local;
use std::io::Read;

#[test]
fn replacement_preserves_open_readers_and_publishes_complete_new_bytes() {
    let directory = tempfile::tempdir().unwrap();
    let local = Local {
        root: directory.path().into(),
    };
    let key = "jobs/running.json";
    let original = br#"{"state":"running","rows":1}"#;
    assert!(local.create(key, original).unwrap());
    let mut retained_readers = Vec::new();
    for rows in 2..=8 {
        retained_readers.push((local.open(key).unwrap(), local.read(key).unwrap().unwrap()));
        let next = format!(r#"{{"state":"running","rows":{rows}}}"#).into_bytes();
        local.update(key, &mut |_| Ok(next.clone())).unwrap();
        assert_eq!(local.read(key).unwrap().unwrap(), next);
    }
    for (mut reader, expected) in retained_readers {
        let mut actual = Vec::new();
        reader.read_to_end(&mut actual).unwrap();
        assert_eq!(actual, expected);
    }
    assert_eq!(
        std::fs::read_dir(directory.path().join("jobs"))
            .unwrap()
            .count(),
        1
    );
}

#[test]
fn creation_does_not_overwrite_objects_or_artifacts() {
    let directory = tempfile::tempdir().unwrap();
    let local = Local {
        root: directory.path().join("objects"),
    };
    assert!(local.create("record", b"first").unwrap());
    assert!(!local.create("record", b"second").unwrap());
    let source = directory.path().join("artifact-source");
    std::fs::write(&source, b"artifact").unwrap();
    local.create_file("record", &source).unwrap();
    assert_eq!(local.read("record").unwrap().unwrap(), b"first");
    local.create_file("artifact", &source).unwrap();
    std::fs::write(&source, b"changed").unwrap();
    local.create_file("artifact", &source).unwrap();
    assert_eq!(local.read("artifact").unwrap().unwrap(), b"artifact");
}

#[cfg(windows)]
#[test]
fn denied_replacement_preserves_target_and_reports_path_and_os_error() {
    use std::os::windows::fs::OpenOptionsExt;
    let directory = tempfile::tempdir().unwrap();
    let local = Local {
        root: directory.path().into(),
    };
    let key = "blocked.json";
    local.create(key, b"original").unwrap();
    // This reader deliberately disallows delete sharing, unlike normal readers.
    let _reader = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(1)
        .open(directory.path().join(key))
        .unwrap();
    let failure = local
        .update(key, &mut |_| Ok(b"replacement".to_vec()))
        .unwrap_err();
    let message = failure.to_string();
    assert!(message.contains("replace local object"), "{message}");
    assert!(message.contains(key), "{message}");
    assert!(message.contains("os error"), "{message}");
    assert_eq!(local.read(key).unwrap().unwrap(), b"original");
    assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(), 2);
}
