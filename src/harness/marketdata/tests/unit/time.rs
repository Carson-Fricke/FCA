use super::*;

#[test]
fn dates_round_trip() {
    for (y, m, d) in [
        (1970, 1, 1),
        (2000, 2, 29),
        (1999, 12, 31),
        (2024, 7, 4),
        (1900, 1, 1),
        (2100, 12, 31),
    ] {
        let date = Date::from_ymd(y, m, d).unwrap();
        assert_eq!(date.ymd(), (y, m, d));
        assert_eq!(Date::from_days(date.days()), date);
    }
    assert_eq!(Date::EPOCH.isoformat(), "1970-01-01");
    assert_eq!(
        Date::from_ymd(2024, 1, 5).unwrap().isoformat(),
        "2024-01-05"
    );
    assert!(Date::from_ymd(2024, 2, 30).is_none());
}

#[test]
fn nanos_floor_to_microseconds() {
    let instant = DateTime::from_micros(1_500);
    assert_eq!(instant.to_nanos(), 1_500_000);
    assert_eq!(DateTime::from_nanos(1_500_499).micros(), 1_500);
    assert_eq!(DateTime::from_nanos(-1).micros(), -1);
}

#[test]
fn date_of_instant_is_utc() {
    let instant = DateTime::from_ymd_hms(2024, 1, 5, 13, 30, 0, 0).unwrap();
    assert_eq!(instant.date().isoformat(), "2024-01-05");
    assert_eq!(
        DateTime::midnight(Date::from_ymd(2024, 1, 5).unwrap()),
        DateTime::from_ymd_hms(2024, 1, 5, 0, 0, 0, 0).unwrap()
    );
}
