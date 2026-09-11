use super::*;

#[test]
fn priorities_match_python_bus() {
    assert_eq!(EventType::Quote.priority(), 0);
    assert_eq!(EventType::Trade.priority(), 1);
    assert_eq!(EventType::Account.priority(), 8);
}
