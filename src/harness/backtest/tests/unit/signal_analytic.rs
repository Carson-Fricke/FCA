use super::*;

#[test]
fn constant_signal_has_constant_envelope() {
    let values = vec![1.0f64; 8];
    let (envelope, _) = analytic_signal(&values).expect("analytic");
    for value in envelope {
        assert!((value - 1.0).abs() < 1e-9);
    }
}

#[test]
fn cosine_envelope_is_flat() {
    let values: Vec<f64> = (0..64)
        .map(|index| (2.0 * std::f64::consts::PI * index as f64 / 16.0).cos())
        .collect();
    let (envelope, _) = analytic_signal(&values).expect("analytic");
    // Away from the circular edges the envelope of a pure cosine is one.
    for value in &envelope[8..56] {
        assert!((value - 1.0).abs() < 1e-6, "{value}");
    }
}

#[test]
fn unwrap_removes_two_pi_jumps() {
    let phase = vec![0.0, 3.0, -3.0, 0.0];
    let unwrapped = unwrap(&phase);
    assert!((unwrapped[0] - 0.0).abs() < 1e-12);
    assert!((unwrapped[1] - 3.0).abs() < 1e-12);
    assert!((unwrapped[2] - (2.0 * std::f64::consts::PI - 3.0)).abs() < 1e-9);
}

#[test]
fn coherence_is_one_for_constant_increment() {
    let phase: Vec<f64> = (0..32).map(|index| 0.25 * index as f64).collect();
    assert!((phase_coherence(&phase) - 1.0).abs() < 1e-9);
}
