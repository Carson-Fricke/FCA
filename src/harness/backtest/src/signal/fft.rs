//! Minimal FFT kernels for the signal module.
//!
//! The scalogram convolution uses a power-of-two length, so an iterative
//! radix-2 Cooley-Tukey transform is exact up to float64 rounding. The analytic
//! signal needs a transform of arbitrary length, so a direct DFT covers that
//! case. Neither function allocates beyond the input/output vectors and both are
//! deterministic.

use std::f64::consts::PI;

/// In-place forward FFT for a power-of-two length.
///
/// # Panics
///
/// Panics when `re.len() != im.len()` or the length is not a power of two.
pub fn fft_in_place(re: &mut [f64], im: &mut [f64]) {
    assert_eq!(re.len(), im.len(), "real and imaginary lengths must match");
    let n = re.len();
    assert!(
        n.is_power_of_two(),
        "radix-2 FFT requires a power-of-two length"
    );
    bit_reverse(re, im);
    let mut len = 2usize;
    while len <= n {
        let angle = -2.0 * PI / len as f64;
        let wlen_re = angle.cos();
        let wlen_im = angle.sin();
        let mut start = 0usize;
        while start < n {
            let mut w_re = 1.0f64;
            let mut w_im = 0.0f64;
            for offset in 0..len / 2 {
                let even = start + offset;
                let odd = even + len / 2;
                let t_re = w_re * re[odd] - w_im * im[odd];
                let t_im = w_re * im[odd] + w_im * re[odd];
                re[odd] = re[even] - t_re;
                im[odd] = im[even] - t_im;
                re[even] += t_re;
                im[even] += t_im;
                let next_re = w_re * wlen_re - w_im * wlen_im;
                let next_im = w_re * wlen_im + w_im * wlen_re;
                w_re = next_re;
                w_im = next_im;
            }
            start += len;
        }
        len <<= 1;
    }
}

/// In-place inverse FFT for a power-of-two length.
///
/// # Panics
///
/// Panics under the same conditions as [`fft_in_place`].
pub fn ifft_in_place(re: &mut [f64], im: &mut [f64]) {
    for value in im.iter_mut() {
        *value = -*value;
    }
    fft_in_place(re, im);
    let n = re.len() as f64;
    for index in 0..re.len() {
        re[index] /= n;
        im[index] = -im[index] / n;
    }
}

fn bit_reverse(re: &mut [f64], im: &mut [f64]) {
    let n = re.len();
    let mut j = 0usize;
    for i in 1..n {
        let mut bit = n >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j |= bit;
        if i < j {
            re.swap(i, j);
            im.swap(i, j);
        }
    }
}

/// Direct forward DFT for an arbitrary length.
#[must_use]
pub fn dft(re_in: &[f64], im_in: &[f64]) -> (Vec<f64>, Vec<f64>) {
    let n = re_in.len();
    let mut re_out = vec![0.0f64; n];
    let mut im_out = vec![0.0f64; n];
    for k in 0..n {
        let mut sum_re = 0.0f64;
        let mut sum_im = 0.0f64;
        for t in 0..n {
            let angle = -2.0 * PI * (k * t % n) as f64 / n as f64;
            let cos = angle.cos();
            let sin = angle.sin();
            sum_re += re_in[t] * cos - im_in[t] * sin;
            sum_im += re_in[t] * sin + im_in[t] * cos;
        }
        re_out[k] = sum_re;
        im_out[k] = sum_im;
    }
    (re_out, im_out)
}

/// Direct inverse DFT for an arbitrary length.
#[must_use]
pub fn idft(re_in: &[f64], im_in: &[f64]) -> (Vec<f64>, Vec<f64>) {
    let n = re_in.len();
    let (mut re_out, mut im_out) = dft(re_in, im_in);
    let scale = n as f64;
    for index in 0..n {
        re_out[index] /= scale;
        im_out[index] = -im_out[index] / scale;
    }
    (re_out, im_out)
}
