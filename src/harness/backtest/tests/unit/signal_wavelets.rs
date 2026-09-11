use super::*;

#[test]
fn highpass_is_quadrature_mirror() {
    let (low, high) = wavelet_filters("la8").expect("filters");
    assert_eq!(low.len(), 8);
    for index in 0..8 {
        let sign = if index % 2 == 0 { 1.0 } else { -1.0 };
        assert!((high[index] - sign * low[7 - index]).abs() < 1e-15);
    }
}

#[test]
fn filter_widths_match_the_closed_form() {
    assert_eq!(level_filter_width(1, 8), 8);
    assert_eq!(level_filter_width(2, 8), 22);
    assert_eq!(level_filter_width(3, 8), 50);
    assert_eq!(max_level(64, "la8").expect("max"), 2);
}

#[test]
fn circular_modwt_is_exactly_invertible() {
    let values: Vec<f64> = (0..32)
        .map(|index| ((index as f64) * 0.7).sin() + ((index as f64) * 0.2).cos())
        .collect();
    let result = modwt(&values, "la8", Some(2), Boundary::Circular).expect("modwt");
    assert_eq!(result.details.len(), 2);
    assert_eq!(result.scaling.len(), values.len());
    let restored = imodwt(&result).expect("imodwt");
    for (a, b) in restored.iter().zip(values.iter()) {
        assert!((a - b).abs() < 1e-9, "inverse mismatch: {a} vs {b}");
    }
}

#[test]
fn circular_mra_sums_to_input() {
    let values: Vec<f64> = (0..32)
        .map(|index| ((index as f64) * 0.7).sin() + ((index as f64) * 0.2).cos())
        .collect();
    let mra = modwt_mra(&values, "db4", Some(2), Boundary::Circular).expect("mra");
    let mut sum = mra.smooth.clone();
    for detail in &mra.details {
        for (slot, value) in sum.iter_mut().zip(detail.iter()) {
            *slot += value;
        }
    }
    for (a, b) in sum.iter().zip(values.iter()) {
        assert!((a - b).abs() < 1e-9);
    }
}

#[test]
fn reflection_pad_matches_numpy() {
    let padded = reflect_pad(&[1.0, 2.0, 3.0], 2);
    assert_eq!(padded, vec![3.0, 2.0, 1.0, 2.0, 3.0, 2.0, 1.0]);
}

#[test]
fn variance_excludes_the_boundary_zone() {
    let values: Vec<f64> = (0..32).map(|index| (index as f64).sin()).collect();
    let result = modwt(&values, "la8", Some(2), Boundary::Reflection).expect("modwt");
    let variance = wavelet_variance(&result);
    assert_eq!(variance.counts.len(), 2);
    assert_eq!(
        variance.counts[0],
        result.length - result.boundary_widths[0]
    );
    assert!(variance.variance[0].is_finite());
}

#[test]
fn unknown_wavelet_is_rejected() {
    assert_eq!(
        wavelet_filters("nope"),
        Err(SignalError::UnknownWavelet("nope".to_string()))
    );
}
