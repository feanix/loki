//! Tests of the seeded alpha noise (`noise.rs`) behind the `_cat` and `_can` mip filters: determinism, the value map,
//! the strength and grain it was matched on, and the `_cat` coverage.

use crate::encode;
use crate::img::Image;
use crate::noise;
use crate::rules::{self, Rule};

fn rule(suffix: &str) -> &'static Rule {
    rules::find(suffix).unwrap_or_else(|| panic!("no class for _{suffix}"))
}

/// A `w` x `h` level whose alpha is `f(x, y)` (colour is a fixed grey, it is not touched by the noise).
fn level(w: usize, h: usize, f: impl Fn(usize, usize) -> u8) -> Vec<u8> {
    let mut v = Vec::with_capacity(w * h * 4);
    for y in 0..h {
        for x in 0..w {
            v.extend_from_slice(&[90, 120, 60, f(x, y)]);
        }
    }
    v
}

fn alphas(v: &[u8]) -> Vec<f64> {
    v.chunks_exact(4).map(|p| p[3] as f64).collect()
}

fn mean_std(x: &[f64]) -> (f64, f64) {
    let m = x.iter().sum::<f64>() / x.len() as f64;
    (
        m,
        (x.iter().map(|v| (v - m) * (v - m)).sum::<f64>() / x.len() as f64).sqrt(),
    )
}

/// Lag-`lag` autocorrelation along x of a `w`-wide field.
fn acx(x: &[f64], w: usize, lag: usize) -> f64 {
    let (m, s) = mean_std(x);
    let (mut sum, mut n) = (0.0, 0.0);
    for row in x.chunks_exact(w) {
        for i in lag..w {
            sum += (row[i] - m) * (row[i - lag] - m);
            n += 1.0;
        }
    }
    sum / n / (s * s)
}

#[test]
fn can_keeps_zero_and_maps_alpha_to_64_plus_half_with_white_noise() {
    let mut v = level(128, 128, |x, _| if x < 16 { 0 } else { 200 });
    noise::add_alpha_noise(&mut v, 128, 128);
    let a = alphas(&v);
    for row in a.chunks_exact(128) {
        assert!(row[..16].iter().all(|&p| p == 0.0), "alpha 0 stays 0");
    }
    let body: Vec<f64> = a.chunks_exact(128).flat_map(|r| r[16..].to_vec()).collect();
    let (m, s) = mean_std(&body);
    assert!((m - 164.0).abs() < 1.0, "mean {m}: 64 + 200 / 2");
    assert!((s - 15.9).abs() < 1.2, "std {s}");
    assert!(acx(&body, 112, 1).abs() < 0.1, "white at 128");
    assert_eq!(v[3 + 4 * 20] as f64, a[20]);
    assert!(
        v.chunks_exact(4).all(|p| p[..3] == [90, 120, 60]),
        "colour untouched"
    );
}

#[test]
fn can_has_no_noise_on_the_single_block_level() {
    let mut v = level(4, 4, |_, _| 100);
    noise::add_alpha_noise(&mut v, 4, 4);
    assert!(alphas(&v).iter().all(|&p| p == 114.0));
}

#[test]
fn noise_is_deterministic_and_depends_on_the_level_size_only() {
    let make = |c: u8| level(64, 64, |x, y| ((x * 3 + y * 5) % 256) as u8 / 2 + c);
    let (mut a, mut b) = (make(10), make(10));
    noise::alpha_noise(&mut a, 64, 64);
    noise::alpha_noise(&mut b, 64, 64);
    assert_eq!(a, b);
    // The same level size gives the same noise field: two flat inputs of different alpha differ by the value
    // map only (`_can`: half the difference), texel for texel, up to rounding.
    let (mut lo, mut hi) = (level(64, 64, |_, _| 100), level(64, 64, |_, _| 120));
    noise::add_alpha_noise(&mut lo, 64, 64);
    noise::add_alpha_noise(&mut hi, 64, 64);
    for (p, q) in lo.chunks_exact(4).zip(hi.chunks_exact(4)) {
        assert!((q[3] as i32 - p[3] as i32 - 10).abs() <= 1);
    }
    // A different level size is another field.
    let (mut s1, mut s2) = (level(32, 32, |_, _| 100), level(16, 64, |_, _| 100));
    noise::add_alpha_noise(&mut s1, 32, 32);
    noise::add_alpha_noise(&mut s2, 16, 64);
    assert_ne!(s1, s2);
}

#[test]
fn cat_is_half_solid_whatever_the_source() {
    let n = 128;
    for (name, f) in [
        (
            "flat 0",
            Box::new(|_: usize, _: usize| 0u8) as Box<dyn Fn(usize, usize) -> u8>,
        ),
        ("flat 255", Box::new(|_, _| 255)),
        (
            "fence 78%",
            Box::new(|x, _| if x % 8 < 6 { 255 } else { 0 }),
        ),
        (
            "fence 25%",
            Box::new(|x, _| if x % 8 < 2 { 255 } else { 0 }),
        ),
        ("ramp", Box::new(|x, _| (x * 2) as u8)),
    ] {
        let mut v = level(n, n, f);
        noise::alpha_noise(&mut v, n, n);
        let a = alphas(&v);
        let cover = a.iter().filter(|&&p| p >= 128.0).count() as f64 / a.len() as f64;
        assert!((cover - 0.5).abs() < 0.03, "{name}: coverage {cover}");
        let (m, _) = mean_std(&a);
        assert!((m - 128.0).abs() < 4.0, "{name}: mean {m}");
    }
}

#[test]
fn cat_flat_gives_a_uniform_alpha_and_a_flat_single_block_gives_255() {
    let mut v = level(128, 128, |_, _| 77);
    noise::alpha_noise(&mut v, 128, 128);
    let (m, s) = mean_std(&alphas(&v));
    assert!(
        (m - 129.7).abs() < 2.0 && (s - 73.5).abs() < 2.0,
        "mean {m} std {s}"
    );
    assert!(acx(&alphas(&v), 128, 1).abs() < 0.1, "white");
    let mut one = level(4, 4, |_, _| 0);
    noise::alpha_noise(&mut one, 4, 4);
    assert!(alphas(&one).iter().all(|&p| p == 255.0));
}

#[test]
fn cat_keeps_the_order_of_a_cut_out_and_the_slope_of_a_ramp() {
    let n = 128;
    // A hard cut-out of two halves: every texel of the lower half is below the upper half's mean.
    let mut v = level(n, n, |x, _| if x < n / 2 { 0 } else { 255 });
    noise::alpha_noise(&mut v, n, n);
    let a = alphas(&v);
    let (lo, hi): (Vec<f64>, Vec<f64>) = (
        a.chunks_exact(n)
            .flat_map(|r| r[..n / 2].to_vec())
            .collect(),
        a.chunks_exact(n)
            .flat_map(|r| r[n / 2..].to_vec())
            .collect(),
    );
    let (ml, mh) = (mean_std(&lo).0, mean_std(&hi).0);
    assert!(
        (ml - 64.0).abs() < 3.0 && (mh - 193.0).abs() < 3.0,
        "{ml} {mh}"
    );
    assert!(
        lo.iter().cloned().fold(0.0, f64::max) < 150.0,
        "little leak"
    );
    // A ramp keeps its slope: the mean of each eighth follows its box value.
    let mut r = level(n, n, |x, _| (x * 2) as u8);
    noise::alpha_noise(&mut r, n, n);
    let a = alphas(&r);
    for k in 1..7 {
        let part: Vec<f64> = a
            .chunks_exact(n)
            .flat_map(|row| row[k * 16..k * 16 + 16].to_vec())
            .collect();
        let want = (k * 16 * 2 + 16) as f64;
        assert!((mean_std(&part).0 - want).abs() < 6.0, "slice {k}");
    }
}

#[test]
fn the_largest_levels_have_a_wider_grain() {
    // The grain depends on the level size (the longer side). ImageToPAA's `_can` noise measures lag 1 / lag 2 of
    // 0.33 / -0.11 at 1024 and 0.76 / 0.31 at 2048 (`research/paa-noise/SPECTRUM41.md`); white below 512.
    for (w, want1, want2) in [(2048, 0.76, 0.31), (1024, 0.33, -0.11), (256, 0.0, 0.0)] {
        let h = 256;
        let mut v = level(w, h, |_, _| 128);
        noise::add_alpha_noise(&mut v, w, h);
        let a = alphas(&v);
        let (r1, r2) = (acx(&a, w, 1), acx(&a, w, 2));
        assert!(
            (r1 - want1).abs() < 0.08 && (r2 - want2).abs() < 0.08,
            "{w}: {r1} {r2}"
        );
        let (_, s) = mean_std(&a);
        assert!((s - 15.9).abs() < 1.5, "{w}: std kept: {s}");
    }
}

#[test]
fn the_fft_matches_the_dft_and_the_roots_are_exact_at_the_quarter_turns() {
    let n = 32;
    let tw = noise::roots(n);
    assert_eq!(tw[0], (1.0, 0.0));
    assert!(
        (tw[n / 4].0).abs() < 1e-15 && (tw[n / 4].1 - 1.0).abs() < 1e-15,
        "quarter turn"
    );
    let x: Vec<f64> = (0..n).map(|i| ((i * 7 + 3) % 11) as f64 - 5.0).collect();
    let (mut re, mut im) = (x.clone(), vec![0.0; n]);
    noise::fft(&mut re, &mut im, &tw);
    for k in 0..n {
        let (mut dr, mut di) = (0.0, 0.0);
        for (j, v) in x.iter().enumerate() {
            let t = 2.0 * std::f64::consts::PI * (j * k) as f64 / n as f64;
            dr += v * t.cos();
            di += v * t.sin();
        }
        assert!(
            (re[k] - dr).abs() < 1e-9 && (im[k] - di).abs() < 1e-9,
            "bin {k}"
        );
    }
}

/// The largest shaped levels are deterministic and have the standard deviation asked for, on a level that is not a
/// power of two (the field is made on the next power of two and cropped).
#[test]
fn shaped_levels_are_deterministic_and_keep_their_strength() {
    let (w, h) = (700, 530);
    let (mut a, mut b) = (level(w, h, |_, _| 128), level(w, h, |_, _| 128));
    noise::add_alpha_noise(&mut a, w, h);
    noise::add_alpha_noise(&mut b, w, h);
    assert_eq!(a, b);
    let (m, s) = mean_std(&alphas(&a));
    assert!((m - 128.0).abs() < 1.0 && (s - 15.9).abs() < 1.5, "{m} {s}");
}

#[test]
fn encoding_a_cat_and_a_can_file_works_at_every_alpha_and_size() {
    for suffix in ["cat", "can", "non"] {
        for n in [4, 8, 16, 64] {
            for f in [0u8, 128, 255] {
                let img = Image {
                    w: n,
                    h: n,
                    rgba: level(n, n, |x, _| f.max((x % 3) as u8)),
                };
                let bytes = encode::encode(img, rule(suffix)).unwrap();
                assert!(crate::paa::read(&bytes).is_ok());
            }
        }
    }
    assert!(
        crate::supported("rock_cat.png")
            && crate::supported("rock_can.png")
            && crate::supported("rock_non.png")
    );
}
