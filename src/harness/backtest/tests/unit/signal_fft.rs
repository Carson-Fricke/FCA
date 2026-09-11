use super::*;

#[test]
fn fft_matches_dft_for_a_short_signal() {
    let signal = [1.0, 2.0, -1.0, 0.5, 3.0, -2.0, 1.5, 0.0];
    let zeros = [0.0f64; 8];
    let (dft_re, dft_im) = dft(&signal, &zeros);
    let mut re = signal;
    let mut im = [0.0f64; 8];
    fft_in_place(&mut re, &mut im);
    for index in 0..8 {
        assert!((re[index] - dft_re[index]).abs() < 1e-9);
        assert!((im[index] - dft_im[index]).abs() < 1e-9);
    }
}

#[test]
fn ifft_inverts_fft() {
    let signal = [1.0, -2.0, 3.0, 0.25];
    let mut re = signal;
    let mut im = [0.0f64; 4];
    fft_in_place(&mut re, &mut im);
    ifft_in_place(&mut re, &mut im);
    for index in 0..4 {
        assert!((re[index] - signal[index]).abs() < 1e-9);
        assert!(im[index].abs() < 1e-9);
    }
}
