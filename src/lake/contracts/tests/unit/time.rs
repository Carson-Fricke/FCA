use super::*;

#[test]
fn date_round_trips_calendar_parts() {
    let date = Date::from_ymd(2024, 2, 29).expect("leap day");
    assert_eq!(date.ymd(), (2024, 2, 29));
    assert_eq!(date.isoformat(), "2024-02-29");
    assert_eq!(Date::parse("2024-02-29"), Some(date));
    assert_eq!(Date::from_ymd(2023, 2, 29), None);
    assert_eq!(Date::from_ymd(2024, 13, 1), None);
}

#[test]
fn date_day_arithmetic_crosses_year_boundaries() {
    let new_year = Date::from_ymd(2024, 1, 1).expect("new year");
    assert_eq!(new_year.add_days(-1).isoformat(), "2023-12-31");
    assert_eq!(new_year.days_until(new_year.add_days(10)), 10);
    assert_eq!(new_year.weekday_index(), 0);
}

#[test]
fn datetime_nanos_round_trip_floors_to_micros() {
    let instant = DateTime::from_nanos(1_500);
    assert_eq!(instant.micros(), 1);
    assert_eq!(instant.to_nanos(), 1_000);
    let negative = DateTime::from_nanos(-1_500);
    assert_eq!(negative.micros(), -2);
}

#[test]
fn datetime_parses_z_and_offset_forms_to_the_same_instant() {
    let z = DateTime::parse_iso_utc("2024-11-27T14:30:00Z").expect("z form");
    let offset = DateTime::parse_iso_utc("2024-11-27T09:30:00-05:00").expect("offset form");
    assert_eq!(z, offset);
    assert_eq!(z.isoformat_seconds_z(), "2024-11-27T14:30:00Z");
    let fractional =
        DateTime::parse_iso_utc("2024-11-27T14:30:00.123456789Z").expect("fraction form");
    assert_eq!(fractional.micros(), z.micros() + 123_456);
}

#[test]
fn datetime_midnight_date_and_seconds_rendering() {
    let date = Date::from_ymd(2024, 1, 2).expect("date");
    let midnight = DateTime::midnight(date);
    assert_eq!(midnight.date(), date);
    assert_eq!(midnight.isoformat_seconds_z(), "2024-01-02T00:00:00Z");
}

#[test]
fn nanos_parsing_and_formatting_round_trip_exactly() {
    let nanos = parse_iso_nanos("2026-09-21T13:30:00.000000001Z").expect("parse");
    assert_eq!(
        nanos,
        parse_iso_nanos("2026-09-21T13:30:00Z").expect("base") + 1
    );
    assert_eq!(format_nanos_utc(nanos), "2026-09-21T13:30:00.000000001Z");
    let micros_boundary = parse_iso_nanos("2026-09-21T13:30:45.703125Z").expect("micros");
    assert_eq!(
        format_nanos_utc(micros_boundary),
        "2026-09-21T13:30:45.703125Z"
    );
    assert_eq!(
        format_nanos_utc(micros_boundary + 1),
        "2026-09-21T13:30:45.703125001Z"
    );
    assert_eq!(
        format_nanos_utc(micros_boundary - 1),
        "2026-09-21T13:30:45.703124999Z"
    );
}

#[test]
fn nanos_accepts_offsets_and_whole_seconds() {
    let z = parse_iso_nanos("2026-09-21T13:30:00Z").expect("z");
    let offset = parse_iso_nanos("2026-09-21T09:30:00-04:00").expect("offset");
    assert_eq!(z, offset);
    assert!(parse_iso_nanos("not-a-time").is_none());
}
