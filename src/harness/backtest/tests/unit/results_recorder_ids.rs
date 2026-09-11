use super::*;

#[test]
fn deterministic_and_versioned() {
    let mut source = EngineIdSource::new(Seed::Text("recorder-seed".to_string())).expect("seed");
    let first = source.next_id("action").expect("id");
    let second = source.next_id("action").expect("id");
    assert_ne!(first, second);
    assert_eq!(first.len(), 36);
    assert_eq!(&first[14..15], "5", "uuid version 5 nibble");
    assert!(matches!(&first[19..20], "8" | "9" | "a" | "b"), "variant");
    assert_eq!(source.issued(), 2);
}

#[test]
fn same_seed_replays_identically() {
    let mut left = EngineIdSource::new(Seed::Int(7)).expect("seed");
    let mut right = EngineIdSource::new(Seed::Int(7)).expect("seed");
    let left_ids: Vec<String> = (0..5).map(|_| left.next_id("fill").expect("id")).collect();
    let right_ids: Vec<String> = (0..5).map(|_| right.next_id("fill").expect("id")).collect();
    assert_eq!(left_ids, right_ids);
}

#[test]
fn kind_changes_identity() {
    let mut source = EngineIdSource::new(Seed::Text("s".to_string())).expect("seed");
    let action = source.next_id("action").expect("id");
    let mut other = EngineIdSource::new(Seed::Text("s".to_string())).expect("seed");
    let fill = other.next_id("fill").expect("id");
    assert_ne!(action, fill);
}

#[test]
fn empty_seed_and_kind_are_rejected() {
    assert_eq!(
        EngineIdSource::new(Seed::Text(String::new())).err(),
        Some(IdError::EmptySeed)
    );
    let mut source = EngineIdSource::new(Seed::Text("s".to_string())).expect("seed");
    assert_eq!(source.next_id(""), Err(IdError::EmptyKind));
}
