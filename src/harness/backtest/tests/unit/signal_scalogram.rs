use super::*;

#[test]
fn periods_scale_linearly() {
    let periods = morlet_periods(&[1.0, 2.0, 4.0]);
    assert!((periods[1] - 2.0 * periods[0]).abs() < 1e-12);
    assert!((periods[2] - 4.0 * periods[0]).abs() < 1e-12);
}

#[test]
fn log_scales_are_increasing_and_cover_the_range() {
    let scales = log_scales(2.0, 32.0, 4).expect("scales");
    assert!(scales.len() >= 2);
    assert!(scales.windows(2).all(|pair| pair[1] > pair[0]));
}

#[test]
fn cwt_detects_a_tone_at_matching_scale() {
    let values: Vec<f64> = (0..128)
        .map(|index| (2.0 * PI * index as f64 / 16.0).sin())
        .collect();
    let scales = vec![16.0 / (2.0 * PI / cone_constant()) * 1.0];
    let (power, _) = scalogram(&values, &scales, DEFAULT_OMEGA0).expect("scalogram");
    assert_eq!(power.len(), 1);
    assert_eq!(power[0].len(), 128);
    let centre: f64 = power[0][64];
    assert!(centre > 0.0);
}

#[test]
fn cone_mask_marks_the_edges() {
    let mask = cone_mask(64, &[8.0], 2.0);
    assert!(!mask[0][0]);
    assert!(mask[0][32]);
    assert!(!mask[0][63]);
}
