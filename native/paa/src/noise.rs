//! Seeded alpha noise for the `_cat`, `_can` and `_non` mip filters.
//!
//! Bohemia's exact noise algorithm for `_cat` (`AlphaNoise`), `_can` (`AddAlphaNoise`) and `_non`
//! (`NormalizeNormalMapNoise`) could not be reverse engineered: ImageToPAA's generator, its seed and its
//! mapping are not recoverable from its output. This module is Loki's own close approximation. It is matched
//! on what ImageToPAA's output measures as, per mip level: the strength of the noise (standard deviation), its
//! grain and spectrum and, for `_cat` and the alpha of `_non`, the share of texels that pass an alpha test at 128.
//! The bytes therefore differ from ImageToPAA's; the behaviour in game does not.
//! Agreement with ImageToPAA, measured on 157 generated cases (50 alpha pictures as `_cat` and as `_can`, 57 normal maps as `_non`) with levels of 16x16 to 4096x4096, square and not
//! (`research/paa-noise/measure_ext.py`): noise standard deviation within 15 percent (levels of 16x16 and up),
//! grain within one texel and `_cat` coverage within 3 percentage points (levels of 64x64 and up). On levels of
//! 32x32 and below ImageToPAA's own coverage swings by up to 5 points with the input and is not followed.
//! Spectrum (`research/paa-noise/spectrum41.py`, results in `SPECTRUM41.md`): the share of the noise power in each
//! radial band of 0-1/64, 1/64-1/32, 1/32-1/16, 1/16-1/8, 1/8-1/4, 1/4-1/2 and 1/2-0.71 cycles per texel is within
//! 0.05 of ImageToPAA's for `_cat`, `_can` and `_non` on the levels of 256 to 2048 (1024 and 2048 pictures, mips 0 to
//! 2; the worst band differs by 0.017). ImageToPAA's alpha noise is band limited: on 1024 and 2048 levels almost
//! no power above 0.5 cycles per texel and almost none below 1/32, so the noise has neither cloudy clumps nor
//! single-texel speckle. Loki's earlier white noise had both, and read as more or less contrasty with distance.
//!
//! What was measured and modelled (ImageToPAA decoded output against the plain filter of the same codec):
//! * Each filter works per mip level, mip 0 included, from the plain box mean `b` of the source alpha at that
//!   level (the chain itself is not fed back). The noise depends on the level size only: the same level size of
//!   two different images gets the same noise field, so the output is the same on every run.
//! * `_can`: alpha 0 stays 0; any other texel becomes `64 + b/2 + noise`. The noise has a standard deviation of
//!   16 (the same on every level, not averaged down); white and bell shaped (excess kurtosis about -0.4) below
//!   512 texels, band limited (Gaussian texels) from 512 up. The 4x4 level (one codec block) gets no noise.
//! * `_cat`: the alpha of the level is rank-equalised. Every texel gets `b + noise` (standard deviation 32, the
//!   spectrum above), the texels are ranked by that value, and the rank becomes the stored alpha spread
//!   evenly over 0..255. The output is therefore about half solid (alpha >= 128) at every level whatever the
//!   source, as ImageToPAA's is, and each texel stays near its source order: a hard cut-out keeps its shape, a
//!   gradient keeps its slope with the grain of that level.
//! * `_non`: the colour is a normal map filtered like `_nohq` (weighted by the source alpha, renormalised at
//!   every level); the alpha is pure `_cat` noise with no relation to the source alpha (correlation 0.00).
//!
//! The generator is a counter hash (splitmix64): no state, no libm, integers and `+ - * / sqrt` on floats only
//! (the Fourier shaping included: its roots of unity are built from half angles), so the result is the same on
//! every machine and every thread count. The per-size radial profiles are in `noise_profile.rs`, fitted by
//! `research/paa-noise/fit_profile41.py`.

use crate::noise_profile;

/// Mixes one 64-bit value (the splitmix64 finaliser).
fn mix(mut z: u64) -> u64 {
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// The seed of a level: its size only, so a level's noise does not depend on the image.
fn level_seed(w: usize, h: usize) -> u64 {
    mix(((w as u64) << 32 | h as u64) ^ 0x5EED_0A1F_A4C3_9B27)
}

/// Bell shaped noise of standard deviation `sigma`: the sum of `terms` (3 or 4) uniform values, centred.
/// One counter hash per texel gives four 16-bit uniforms.
fn bell(seed: u64, index: usize, terms: usize, sigma: f64) -> f64 {
    let r = mix(seed.wrapping_add((index as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15)));
    let sum: u32 = (0..terms).map(|t| ((r >> (16 * t)) & 0xFFFF) as u32).sum();
    // Uniform on 0..65536 has variance 65536^2 / 12; the sum of `terms` of them has `terms` times that.
    let centred = sum as f64 - terms as f64 * 32767.5;
    let unit = centred / (65536.0 * (terms as f64 / 12.0).sqrt());
    unit * sigma
}

/// Terms of the `_can` noise (excess kurtosis -0.4, as measured).
const CAN_TERMS: usize = 3;
/// Standard deviation of the `_can` noise before the codec; the decoded noise measures 16.1.
const CAN_SIGMA: f64 = 15.9;
/// Terms of the `_cat` pre-rank noise (its tails set how far ranks leak between source classes).
const CAT_TERMS: usize = 4;
/// Standard deviation of the `_cat` pre-rank noise, on the 0..255 alpha scale.
const CAT_SIGMA: f64 = 32.0;
/// Width, in alpha levels, of the groups of equal rank key in `_cat`: texels in one group share the rank of
/// the group's last texel. ImageToPAA's output sits 1 to 3 points above half solid, as such ties give.
const CAT_TIE: f64 = 2.0;
/// `_cat` noise reaches at most this many alpha levels either way (4 terms, 32 sigma: 2 * sqrt(3) * 32).
const CAT_REACH: i32 = 112;

/// The grain of the 4096 levels (not measured in the Fourier model below). ImageToPAA's noise at 4096 has neighbour
/// correlation 0.94 at lag 1 and 0.78 at lag 2; the same comes from convolving the white field with the triangle of
/// two 5-tap boxes in x and y, scaled to keep the standard deviation.
const TAPS_4096: [i32; 9] = [1, 2, 3, 4, 5, 4, 3, 2, 1];

/// Index `i + tap - half` wrapped into `0..n` (the field is periodic).
fn wrap(i: usize, tap: usize, half: usize, n: usize) -> usize {
    (i as isize + tap as isize - half as isize).rem_euclid(n as isize) as usize
}

/// A white field smoothed with `taps` in x and y (periodic), scaled to keep the standard deviation.
fn smoothed(mut f: Vec<f32>, w: usize, h: usize, taps: &[i32]) -> Vec<f32> {
    let half = taps.len() / 2;
    let norm = 1.0 / taps.iter().map(|&t| (t * t) as f32).sum::<f32>();
    let mut tmp = vec![0f32; w * h];
    for y in 0..h {
        for x in 0..w {
            let mut acc = 0f32;
            for (j, &t) in taps.iter().enumerate() {
                acc += t as f32 * f[y * w + wrap(x, j, half, w)];
            }
            tmp[y * w + x] = acc;
        }
    }
    for y in 0..h {
        for x in 0..w {
            let mut acc = 0f32;
            for (j, &t) in taps.iter().enumerate() {
                acc += t as f32 * tmp[wrap(y, j, half, h) * w + x];
            }
            f[y * w + x] = acc * norm;
        }
    }
    f
}

/// Which measured noise a field imitates: the `_cat` and `_non` alpha, or the `_can` alpha.
#[derive(Clone, Copy)]
enum Kind {
    Cat,
    Can,
}

/// Roots of unity for a transform of `n` (a power of two): `(cos, sin)` of `2 pi j / n` for `j < n / 2`. Built from
/// `+ - * / sqrt` only (half angles, then products of them), so the table is the same on every machine.
pub(crate) fn roots(n: usize) -> Vec<(f64, f64)> {
    let levels = n.trailing_zeros() as usize;
    // base[k - 1] = (cos, sin) of 2 pi / 2^k: a half turn, a quarter turn, then half angles.
    let mut base: Vec<(f64, f64)> = vec![(-1.0, 0.0), (0.0, 1.0)];
    for k in 2..levels {
        let half_c = ((1.0 + base[k - 1].0) / 2.0).sqrt();
        base.push((half_c, base[k - 1].1 / (2.0 * half_c)));
    }
    (0..n / 2)
        .map(|j| {
            let (mut c, mut s) = (1.0, 0.0);
            for b in 0..levels {
                if (j >> b) & 1 == 1 {
                    // bit b of j is the angle 2 pi / 2^(levels - b)
                    let (bc, bs) = base[levels - b - 1];
                    (c, s) = (c * bc - s * bs, c * bs + s * bc);
                }
            }
            (c, s)
        })
        .collect()
}

/// In-place inverse complex FFT (no scaling) of `re`, `im` (a power of two points) with the roots `tw` of that size.
pub(crate) fn fft(re: &mut [f64], im: &mut [f64], tw: &[(f64, f64)]) {
    let n = re.len();
    let mut j = 0;
    for i in 1..n {
        let mut bit = n >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if i < j {
            re.swap(i, j);
            im.swap(i, j);
        }
    }
    let mut len = 2;
    while len <= n {
        let step = n / len;
        for start in (0..n).step_by(len) {
            for k in 0..len / 2 {
                let (wc, ws) = tw[k * step];
                let (a, b) = (start + k, start + k + len / 2);
                let (tr, ti) = (re[b] * wc - im[b] * ws, re[b] * ws + im[b] * wc);
                re[b] = re[a] - tr;
                im[b] = im[a] - ti;
                re[a] += tr;
                im[a] += ti;
            }
        }
        len <<= 1;
    }
}

/// Table value at radius `r` (cycles per texel), linear between the bin centres.
fn profile_at(table: &[f64; noise_profile::BINS], r: f64) -> f64 {
    let x = (r * 128.0 - 0.5).clamp(0.0, (noise_profile::BINS - 1) as f64);
    let i = (x as usize).min(noise_profile::BINS - 2);
    let t = x - i as f64;
    table[i] * (1.0 - t) + table[i + 1] * t
}

/// The shaped noise of a `w` x `h` level (longer side 512 to 4095), standard deviation `sigma`: random complex
/// amplitudes on a power-of-two grid, each with the power of the measured radial profile of the level size, made
/// real by mirroring, transformed back and cropped. The texels are close to Gaussian (a sum of many terms).
fn shaped_field(seed: u64, w: usize, h: usize, kind: Kind, sigma: f64) -> Vec<f32> {
    let (p, q) = (w.next_power_of_two(), h.next_power_of_two());
    let class = match w.max(h) {
        2048.. => 2,
        1024.. => 1,
        _ => 0,
    };
    let table = match kind {
        Kind::Cat => &noise_profile::CAT[class],
        Kind::Can => &noise_profile::CAN[class],
    };
    let (mut re, mut im) = (vec![0f64; p * q], vec![0f64; p * q]);
    let mut energy = 0.0;
    for ky in 0..q {
        for kx in 0..p {
            let i = ky * p + kx;
            let m = ((q - ky) % q) * p + (p - kx) % p; // the mirrored bin
            if i > m || i == 0 {
                continue; // the upper half is set from its partner; no mean
            }
            let fx = kx.min(p - kx) as f64 / p as f64;
            let fy = ky.min(q - ky) as f64 / q as f64;
            let amp = profile_at(table, (fx * fx + fy * fy).sqrt()).sqrt();
            let a = bell(seed, 2 * i, 4, 1.0) * amp;
            let b = bell(seed, 2 * i + 1, 4, 1.0) * amp;
            if i == m {
                re[i] = a; // its own mirror: real
                energy += a * a;
            } else {
                (re[i], im[i], re[m], im[m]) = (a, b, a, -b);
                energy += 2.0 * (a * a + b * b);
            }
        }
    }
    // Unscaled inverse transform: the texel variance is the sum of |amplitude|^2.
    let scale = sigma / energy.sqrt();
    for (r, i) in re.iter_mut().zip(im.iter_mut()) {
        *r *= scale;
        *i *= scale;
    }
    let (tp, tq) = (roots(p), roots(q));
    for row in 0..q {
        let span = row * p..(row + 1) * p;
        fft(&mut re[span.clone()], &mut im[span], &tp);
    }
    let (mut cr, mut ci) = (vec![0f64; q], vec![0f64; q]);
    for x in 0..w {
        for y in 0..q {
            (cr[y], ci[y]) = (re[y * p + x], im[y * p + x]);
        }
        fft(&mut cr, &mut ci, &tq);
        for y in 0..h {
            re[y * p + x] = cr[y];
        }
    }
    (0..h)
        .flat_map(|y| (0..w).map(move |x| (y, x)))
        .map(|(y, x)| re[y * p + x] as f32)
        .collect()
}

/// The noise of every texel of a `w` x `h` level (row-major) for `kind`, standard deviation `sigma`. Levels below
/// 512 are white (bell shaped, `terms` uniforms), 512 to 4095 follow ImageToPAA's measured spectrum
/// (`shaped_field`), 4096 and up are smoothed white (`TAPS_4096`).
fn noise_field(seed: u64, w: usize, h: usize, kind: Kind, terms: usize, sigma: f64) -> Vec<f32> {
    let white = || {
        (0..w * h)
            .map(|i| bell(seed, i, terms, sigma) as f32)
            .collect()
    };
    match w.max(h) {
        4096.. => smoothed(white(), w, h, &TAPS_4096),
        512.. => shaped_field(seed, w, h, kind, sigma),
        _ => white(),
    }
}

/// `_can` (`AddAlphaNoise`): alpha of the level `b` becomes 0 if it is 0, else `64 + b/2` plus white noise.
/// `rgba` is the level (RGBA, alpha in every fourth byte); the 4x4 level is left without noise.
pub(crate) fn add_alpha_noise(rgba: &mut [u8], w: usize, h: usize) {
    let quiet = w == 4 && h == 4;
    let field = if quiet {
        Vec::new()
    } else {
        noise_field(level_seed(w, h), w, h, Kind::Can, CAN_TERMS, CAN_SIGMA)
    };
    for (i, p) in rgba.chunks_exact_mut(4).enumerate() {
        if p[3] == 0 {
            continue;
        }
        let n = field.get(i).copied().unwrap_or(0.0) as f64;
        p[3] = (64.0 + p[3] as f64 / 2.0 + n).round().clamp(0.0, 255.0) as u8;
    }
}

/// `_cat` (`AlphaNoise`): the alpha of the level is replaced by the rank of `b + noise` over the level, spread
/// evenly over 0..255 (equal keys share the highest rank of their group). The 4x4 level gets no noise, so a flat
/// one is all 255, as in ImageToPAA's output.
pub(crate) fn alpha_noise(rgba: &mut [u8], w: usize, h: usize) {
    let n = w * h;
    if n == 0 {
        return;
    }
    let quiet = w == 4 && h == 4;
    let field = if quiet {
        Vec::new()
    } else {
        noise_field(level_seed(w, h), w, h, Kind::Cat, CAT_TERMS, CAT_SIGMA)
    };
    let buckets = ((255.0 + 2.0 * CAT_REACH as f64) / CAT_TIE) as usize + 2;
    let keys: Vec<u32> = rgba
        .chunks_exact(4)
        .enumerate()
        .map(|(i, p)| {
            let noise = field.get(i).copied().unwrap_or(0.0) as f64;
            let v = ((p[3] as f64 + noise + CAT_REACH as f64) / CAT_TIE).floor();
            v.clamp(0.0, (buckets - 1) as f64) as u32
        })
        .collect();
    // Counting sort: cumulative counts give each key the number of texels at or below it.
    let mut upto = vec![0u32; buckets];
    for &k in &keys {
        upto[k as usize] += 1;
    }
    for b in 1..buckets {
        upto[b] += upto[b - 1];
    }
    for (p, &k) in rgba.chunks_exact_mut(4).zip(&keys) {
        p[3] = (upto[k as usize] as u64 * 256 / n as u64).min(255) as u8;
    }
}
