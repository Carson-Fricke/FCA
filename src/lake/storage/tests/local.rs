//! Logical object listings and reads remain atomic during local replacement.
use lake_storage::{BlobStore, LocalFilesystemStore, ReadableBlobStore};
use std::{
    io::Write,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Barrier,
    },
    time::{Duration, Instant},
};

#[test]
fn local_replacements_expose_only_committed_objects_for_both_input_forms() {
    let directory = tempfile::tempdir().unwrap();
    let store = Arc::new(LocalFilesystemStore::new(directory.path()));
    store.replace("publications/current.json", b"old").unwrap();
    // An unfinished transport write is physically present but has no object address.
    let private = directory.path().join(".fca-staging");
    std::fs::create_dir_all(&private).unwrap();
    std::fs::write(private.join("unfinished.tmp"), b"partial").unwrap();
    assert_eq!(
        store.list_prefix("").unwrap(),
        vec!["publications/current.json"]
    );
    assert!(store.read(".FCA-STAGING/unfinished.tmp").is_err());
    assert!(store.replace(".fca-staging/injected", b"data").is_err());
    let bytes = vec![b'x'; 2 * 1024 * 1024];
    let mut source = tempfile::NamedTempFile::new().unwrap();
    source.write_all(&bytes).unwrap();
    let path = source.path().to_path_buf();
    let ready = Arc::new(Barrier::new(2));
    let finished = Arc::new(AtomicBool::new(false));
    let writer = {
        let store = store.clone();
        let ready = ready.clone();
        let finished = finished.clone();
        std::thread::spawn(move || {
            ready.wait();
            for index in 0..16 {
                if index % 2 == 0 {
                    store.replace("publications/current.json", &bytes).unwrap();
                } else {
                    store
                        .replace_file("publications/current.json", &path)
                        .unwrap();
                }
            }
            finished.store(true, Ordering::Release);
        })
    };
    ready.wait();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !finished.load(Ordering::Acquire) {
        assert!(
            Instant::now() < deadline,
            "replacement must finish without a namespace race"
        );
        assert_eq!(
            store.list_prefix("publications").unwrap(),
            vec!["publications/current.json"]
        );
    }
    writer.join().unwrap();
    assert_eq!(
        store.read("publications/current.json").unwrap().len(),
        2 * 1024 * 1024
    );
    assert_eq!(
        store.list_prefix("").unwrap(),
        vec!["publications/current.json"]
    );
}
