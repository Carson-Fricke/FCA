use super::smoke_roundtrip;

#[test]
fn smoke_roundtrip_returns_its_input() {
    assert_eq!(smoke_roundtrip(0), 0);
    assert_eq!(smoke_roundtrip(42), 42);
    assert_eq!(smoke_roundtrip(-7), -7);
    assert_eq!(smoke_roundtrip(i64::MAX), i64::MAX);
}
