//! DXT1 (BC1) and DXT5 (BC3) block compressor and decoder.
//!
//! Written from the public block layout (SPEC section 6, Wikipedia S3TC), not
//! from any other encoder. Colour fit: principal axis of the 16 texels gives
//! the first endpoint guess, then a few rounds of least-squares endpoint
//! refinement on the chosen indices, keeping only improvements. Alpha block:
//! same idea on one channel, trying both the 8-level and the 6-level+0/255
//! modes. Solid-colour blocks use a lookup of the best 5/6-bit endpoint pair
//! so flat UI colours are not smeared by quantisation.
//!
//! Palette interpolation uses integer (2a+b)/3 on the 8-bit expanded endpoints;
//! the encoder and the decoder share it so PSNR measures the codec, not a mismatch.
//!
//! The encoder is deterministic and single-threaded: the same pixels give the same bytes
//! (the golden tests in golden.rs pin that), so every constant below is part of the output.

use crate::error::Error;
use std::sync::OnceLock;

/// Which block format to emit.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Format {
    /// 8 byte blocks, opaque colour (or 1-bit alpha when `punch` is set).
    Dxt1 {
        /// Texels with alpha under 128 become transparent (needs the 3-colour block mode).
        punch: bool,
    },
    /// 16 byte blocks: interpolated alpha + colour.
    Dxt5,
}

type Rgb = [i32; 3];

/// Texels with alpha at or above this are opaque for DXT1 punch-through (the midpoint).
const OPAQUE_ALPHA_MIN: u8 = 128;

/// Expand a RGB565 endpoint to 8 bits per channel by bit replication (the high
/// bits are copied into the low bits, so 31 maps to 255 and 0 to 0), the usual way GPUs decode it.
fn expand(c: u16) -> Rgb {
    let (r, g, b) = (
        ((c >> 11) & 31) as i32,
        ((c >> 5) & 63) as i32,
        (c & 31) as i32,
    );
    [
        (r << 3) | (r >> 2),
        (g << 2) | (g >> 4),
        (b << 3) | (b >> 2),
    ]
}

/// Round an 8-bit-range colour to the nearest RGB565 value.
fn quant(r: f32, g: f32, b: f32) -> u16 {
    let q = |v: f32, m: f32| (v.clamp(0.0, 255.0) * m / 255.0 + 0.5) as u16;
    (q(r, 31.0) << 11) | (q(g, 63.0) << 5) | q(b, 31.0)
}

/// Palette of a colour block. `mode3` = c0 <= c1 layout (midpoint + transparent).
fn palette(c0: u16, c1: u16, mode3: bool) -> [Rgb; 4] {
    let (a, b) = (expand(c0), expand(c1));
    let mut p = [a, b, [0; 3], [0; 3]];
    if mode3 {
        for k in 0..3 {
            p[2][k] = (a[k] + b[k]) / 2;
        }
    } else {
        for k in 0..3 {
            p[2][k] = (2 * a[k] + b[k]) / 3;
            p[3][k] = (a[k] + 2 * b[k]) / 3;
        }
    }
    p
}

fn dist2(a: &Rgb, b: &Rgb) -> i32 {
    let d = [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
    d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
}

/// Best palette index per texel (only the first `n` palette entries) and total error.
/// Texels flagged transparent (mode3 only) take index 3 at no cost.
fn assign(px: &[Rgb; 16], opaque: &[bool; 16], pal: &[Rgb; 4], n: usize) -> ([u8; 16], i64) {
    let mut idx = [3u8; 16];
    let mut err = 0i64;
    for i in 0..16 {
        if !opaque[i] {
            continue;
        }
        let (mut best, mut bi) = (i32::MAX, 0u8);
        for (k, p) in pal.iter().enumerate().take(n) {
            let d = dist2(&px[i], p);
            if d < best {
                best = d;
                bi = k as u8;
            }
        }
        idx[i] = bi;
        err += best as i64;
    }
    (idx, err)
}

/// Pack sixteen 2-bit indices, texel 0 in the low bits.
fn pack_idx(idx: &[u8; 16]) -> u32 {
    idx.iter()
        .enumerate()
        .fold(0u32, |acc, (i, &v)| acc | ((v as u32) << (2 * i)))
}

/// Per 8-bit value: best 5-bit and 6-bit endpoint pair when the texel sits at palette index 2.
struct SolidTable {
    t5: [(u8, u8); 256],
    t6: [(u8, u8); 256],
}

/// Build (once) the table that lets a flat colour hit its value through palette index 2.
///
/// Why: a flat block quantised to a single RGB565 value is off by up to half a 5/6-bit step,
/// but index 2 is (2*lo + hi)/3 of two different endpoints, which has finer steps. For each
/// 8-bit value this finds the (lo, hi) pair, by exhaustive search, whose index-2 colour is
/// closest. Ties keep the first pair found (lo-major order), which is part of the output.
fn solid_table() -> &'static SolidTable {
    static T: OnceLock<SolidTable> = OnceLock::new();
    T.get_or_init(|| {
        let build = |bits: u32| {
            let n = 1i32 << bits;
            let ex = |v: i32| {
                if bits == 5 {
                    (v << 3) | (v >> 2)
                } else {
                    (v << 2) | (v >> 4)
                }
            };
            let mut t = [(0u8, 0u8); 256];
            for v in 0..256i32 {
                // i32::MAX: any real error beats the starting value.
                let mut best = (i32::MAX, 0, 0);
                for lo in 0..n {
                    for hi in 0..n {
                        let m = (2 * ex(lo) + ex(hi)) / 3;
                        let e = (m - v).abs();
                        if e < best.0 {
                            best = (e, lo, hi);
                        }
                    }
                }
                t[v as usize] = (best.1 as u8, best.2 as u8);
            }
            t
        };
        SolidTable {
            t5: build(5),
            t6: build(6),
        }
    })
}

/// Fit a colour block. `opaque[i]` false = transparent texel: only DXT1 punch-through passes any
/// (other formats pass all true, so alpha never affects their colour fit).
/// Returns (c0, c1, packed indices).
fn fit_colour(px: &[Rgb; 16], opaque: &[bool; 16]) -> (u16, u16, u32) {
    // Transparent texels need the 3-colour layout (index 3 = transparent); without any, the
    // 4-colour layout is used even for punch-through formats.
    let mode3 = opaque.iter().any(|&o| !o);
    // Texels that take part in the fit. A block of one repeated colour gets the exact
    // solid-colour treatment; anything else is fitted along its principal axis and then refined.
    let live: Vec<usize> = (0..16).filter(|&i| opaque[i]).collect();
    if live.is_empty() {
        // Fully transparent block: c0 == c1 == 0 is the 3-colour layout, all indices 3 (transparent).
        return (0, 0, 0xFFFF_FFFF);
    }

    if live.iter().all(|&i| px[i] == px[live[0]]) {
        return solid_block(px[live[0]], opaque, mode3);
    }
    let (e0, e1) = principal_axis_endpoints(px, &live);
    refine_endpoints(px, opaque, &live, mode3, e0, e1)
}

/// Fit a block whose live texels are one colour `c`: either one flat RGB565 value, or (4-colour
/// layout only) the table pair that hits `c` through index 2, whichever has the smaller error.
fn solid_block(c: Rgb, opaque: &[bool; 16], mode3: bool) -> (u16, u16, u32) {
    let flat = quant(c[0] as f32, c[1] as f32, c[2] as f32);
    // Candidate 1: both endpoints the flat value, texels on index 0 (the 3-colour palette reads
    // the same for c0 == c1, so error is the distance to endpoint 0).
    let pal0 = palette(flat, flat, true);
    let mut best = (dist2(&c, &pal0[0]) as i64, flat, flat, 0u8);
    if !mode3 {
        // Candidate 2: table endpoints, texels on index 2.
        let t = solid_table();
        let (r, g, b) = (
            t.t5[c[0] as usize],
            t.t6[c[1] as usize],
            t.t5[c[2] as usize],
        );
        let pack = |r: u8, g: u8, b: u8| ((r as u16) << 11) | ((g as u16) << 5) | b as u16;
        let (c0, c1) = (pack(r.0, g.0, b.0), pack(r.1, g.1, b.1));
        let pal = palette(c0, c1, false);
        let e = dist2(&c, &pal[2]) as i64;
        if e < best.0 && c0 != c1 {
            // Keep c0 > c1 (4-colour layout): swapping the endpoints turns index 2 into index 3.
            best = if c0 > c1 {
                (e, c0, c1, 2)
            } else {
                (e, c1, c0, 3)
            };
        }
    }
    let mut idx = [0u8; 16];
    for (i, v) in idx.iter_mut().enumerate() {
        *v = if opaque[i] { best.3 } else { 3 };
    }
    // c0 == c1 reads as the 3-colour layout, where only index 0 is the texel colour; candidate 1
    // (the only one that can have c0 == c1) already puts its texels on index 0.
    (best.1, best.2, pack_idx(&idx))
}

/// First endpoint guess for a block with varying colour: the extremes of the live texels
/// along their principal axis (largest eigenvector of the covariance, by power iteration).
fn principal_axis_endpoints(px: &[Rgb; 16], live: &[usize]) -> ([f32; 3], [f32; 3]) {
    let cnt = live.len() as f32;
    let mut mean = [0f32; 3];
    for &i in live {
        for (m, &v) in mean.iter_mut().zip(px[i].iter()) {
            *m += v as f32;
        }
    }
    for m in mean.iter_mut() {
        *m /= cnt;
    }
    let mut cov = [[0f32; 3]; 3];
    for &i in live {
        let d = [
            px[i][0] as f32 - mean[0],
            px[i][1] as f32 - mean[1],
            px[i][2] as f32 - mean[2],
        ];
        for a in 0..3 {
            for b in 0..3 {
                cov[a][b] += d[a] * d[b];
            }
        }
    }
    // Seed: the covariance column of the channel with the most variance. The tiny offset keeps
    // the seed from being the zero vector.
    let diag_max = (0..3)
        .max_by(|&a, &b| cov[a][a].partial_cmp(&cov[b][b]).unwrap())
        .unwrap();
    let mut axis = [
        cov[0][diag_max] + 1e-3,
        cov[1][diag_max] + 1e-3,
        cov[2][diag_max] + 1e-3,
    ];
    // Six rounds are plenty for a 3x3 matrix with the seed already close to the top eigenvector.
    for _ in 0..6 {
        let v = [
            cov[0][0] * axis[0] + cov[0][1] * axis[1] + cov[0][2] * axis[2],
            cov[1][0] * axis[0] + cov[1][1] * axis[1] + cov[1][2] * axis[2],
            cov[2][0] * axis[0] + cov[2][1] * axis[1] + cov[2][2] * axis[2],
        ];
        let l = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
        if l < 1e-6 {
            // Degenerate covariance: keep the last usable axis.
            break;
        }
        axis = [v[0] / l, v[1] / l, v[2] / l];
    }
    let (mut tmin, mut tmax) = (f32::MAX, f32::MIN);
    for &i in live {
        let t = (px[i][0] as f32 - mean[0]) * axis[0]
            + (px[i][1] as f32 - mean[1]) * axis[1]
            + (px[i][2] as f32 - mean[2]) * axis[2];
        tmin = tmin.min(t);
        tmax = tmax.max(t);
    }
    (
        [
            mean[0] + axis[0] * tmax,
            mean[1] + axis[1] * tmax,
            mean[2] + axis[2] * tmax,
        ],
        [
            mean[0] + axis[0] * tmin,
            mean[1] + axis[1] * tmin,
            mean[2] + axis[2] * tmin,
        ],
    )
}

/// Solve the 2x2 normal equations of the endpoint fit for one channel: minimise
/// sum (a*e0 + b*e1 - v)^2 given aa = sum a*a, ab = sum a*b, bb = sum b*b, ra = sum a*v,
/// rb = sum b*v. `None` when the system is singular (all texels on one palette weight).
fn solve_endpoints(aa: f32, ab: f32, bb: f32, ra: f32, rb: f32) -> Option<(f32, f32)> {
    let det = aa * bb - ab * ab;
    // Near-zero determinant: the endpoints cannot be told apart (every texel sits on one palette
    // weight), so there is no unique least-squares answer.
    if det.abs() < 1e-4 {
        return None;
    }
    Some(((ra * bb - rb * ab) / det, (rb * aa - ra * ab) / det))
}

/// Improve the endpoint guess `e0`, `e1`: quantise, assign indices, then up to 4 rounds of
/// least-squares endpoints for the current indices, keeping a round only if the error drops.
/// Returns (c0, c1, packed indices); transparent texels always get index 3.
fn refine_endpoints(
    px: &[Rgb; 16],
    opaque: &[bool; 16],
    live: &[usize],
    mode3: bool,
    mut e0: [f32; 3],
    mut e1: [f32; 3],
) -> (u16, u16, u32) {
    let n = if mode3 { 3 } else { 4 };
    // Weight of endpoint 0 per palette index (the rest goes to endpoint 1). In the 3-colour layout
    // index 3 is the transparent marker, never a colour, so its 0.0 is never used: those texels
    // are not in `live`, and so not in the fit.
    let w: [f32; 4] = if mode3 {
        [1.0, 0.0, 0.5, 0.0]
    } else {
        [1.0, 0.0, 2.0 / 3.0, 1.0 / 3.0]
    };

    // The layout is selected by the endpoint order: 4-colour needs c0 > c1, 3-colour c0 <= c1.
    let order = |a: u16, b: u16| -> (u16, u16) {
        if mode3 == (a <= b) {
            (a, b)
        } else {
            (b, a)
        }
    };
    let evaluate = |c0: u16, c1: u16| -> ([u8; 16], i64) {
        if !mode3 && c0 == c1 {
            // c0 == c1 is read as 3-colour mode: only index 0 is safe.
            let pal = palette(c0, c1, false);
            let (_, err) = assign(px, opaque, &pal, 1);
            return ([0u8; 16], err);
        }
        let pal = palette(c0, c1, mode3);
        assign(px, opaque, &pal, n)
    };

    let (mut c0, mut c1) = order(quant(e0[0], e0[1], e0[2]), quant(e1[0], e1[1], e1[2]));
    let (mut best_idx, mut best_err) = evaluate(c0, c1);
    for _ in 0..4 {
        if best_err == 0 {
            break;
        }
        // Least-squares endpoints for the current assignment, all three channels at once.
        let (mut aa, mut ab, mut bb) = (0f32, 0f32, 0f32);
        let mut ra = [0f32; 3];
        let mut rb = [0f32; 3];
        for &i in live {
            let k = best_idx[i] as usize;
            let (a, b) = (w[k], 1.0 - w[k]);
            aa += a * a;
            ab += a * b;
            bb += b * b;
            for ch in 0..3 {
                ra[ch] += a * px[i][ch] as f32;
                rb[ch] += b * px[i][ch] as f32;
            }
        }
        // The determinant does not depend on the channel: singular for one means for all
        // (every texel on the same index weight), and then there is nothing to refine.
        let sol = [0, 1, 2].map(|ch| solve_endpoints(aa, ab, bb, ra[ch], rb[ch]));
        let [Some(s0), Some(s1), Some(s2)] = sol else {
            break;
        };
        for (ch, (v0, v1)) in [s0, s1, s2].into_iter().enumerate() {
            (e0[ch], e1[ch]) = (v0, v1);
        }
        let (n0, n1) = order(quant(e0[0], e0[1], e0[2]), quant(e1[0], e1[1], e1[2]));
        let (idx, err) = evaluate(n0, n1);
        if err < best_err {
            (c0, c1, best_idx, best_err) = (n0, n1, idx, err);
        } else {
            break;
        }
    }
    (c0, c1, pack_idx(&best_idx))
}

/// The 8 alpha levels of a DXT5 alpha block. `a0 > a1`: 8 interpolated levels; otherwise 6
/// interpolated levels plus fixed 0 and 255.
fn alpha_levels(a0: u8, a1: u8) -> [u8; 8] {
    let (x, y) = (a0 as i32, a1 as i32);
    let mut p = [a0, a1, 0, 0, 0, 0, 0, 0];
    // The + 3 (and + 2 below) is half the divisor: it rounds to nearest instead of truncating.
    // The decoder uses this same table, so encoder and decoder agree.
    if a0 > a1 {
        for k in 1..7 {
            p[k + 1] = (((7 - k as i32) * x + k as i32 * y + 3) / 7) as u8;
        }
    } else {
        for k in 1..5 {
            p[k + 1] = (((5 - k as i32) * x + k as i32 * y + 2) / 5) as u8;
        }
        p[6] = 0;
        p[7] = 255;
    }
    p
}

/// Best alpha level index per texel, and the squared error.
fn alpha_assign(a: &[u8; 16], pal: &[u8; 8]) -> ([u8; 16], i64) {
    let mut idx = [0u8; 16];
    let mut err = 0i64;
    for i in 0..16 {
        let (mut best, mut bi) = (i32::MAX, 0u8);
        for (k, &level) in pal.iter().enumerate() {
            let d = (a[i] as i32 - level as i32).abs();
            if d < best {
                best = d;
                bi = k as u8;
            }
        }
        idx[i] = bi;
        err += (best * best) as i64;
    }
    (idx, err)
}

/// Best alpha endpoints found so far for one block.
struct AlphaFit {
    err: i64,
    a0: u8,
    a1: u8,
    idx: [u8; 16],
}

impl AlphaFit {
    /// Try the endpoint pair `a0`, `a1`; keep it when its error is strictly lower.
    fn consider(&mut self, a: &[u8; 16], a0: u8, a1: u8) {
        let (idx, err) = alpha_assign(a, &alpha_levels(a0, a1));
        if err < self.err {
            *self = AlphaFit { err, a0, a1, idx };
        }
    }
}

/// Fit an alpha block: returns 8 bytes (two endpoints + 48 bits of 3-bit indices).
fn fit_alpha(a: &[u8; 16]) -> [u8; 8] {
    let (lo, hi) = (*a.iter().min().unwrap(), *a.iter().max().unwrap());
    // i64::MAX: the first candidate always wins.
    let mut best = AlphaFit {
        err: i64::MAX,
        a0: lo,
        a1: hi,
        idx: [0; 16],
    };
    if lo == hi {
        // Flat alpha: a single endpoint pair reproduces it exactly, no search or refinement needed.
        best.consider(a, lo, hi);
    } else {
        // 8-level mode: endpoints at the extremes, then refine.
        best.consider(a, hi, lo);
        // 6-level mode with implicit 0 and 255: endpoints cover the values that are not 0/255.
        let inner: Vec<u8> = a.iter().copied().filter(|&v| v != 0 && v != 255).collect();
        if let (Some(&ilo), Some(&ihi)) = (inner.iter().min(), inner.iter().max()) {
            best.consider(a, ilo, ihi);
        } else {
            best.consider(a, 0, 255); // pure 0/255 content
        }
        // Refinement of the best so far by least squares on its indices (up to 3 rounds, a
        // round is kept only when the error drops, so more rounds can never hurt).
        for _ in 0..3 {
            let (a0, a1) = (best.a0, best.a1);
            let six = a0 <= a1;
            // Weight of endpoint 0 per index. In the 6-level mode indices 6 and 7 are fixed
            // 0 and 255, not endpoint blends: the -1 marks them as skipped.
            let w: [f32; 8] = if six {
                [1.0, 0.0, 0.8, 0.6, 0.4, 0.2, -1.0, -1.0]
            } else {
                [
                    1.0,
                    0.0,
                    6.0 / 7.0,
                    5.0 / 7.0,
                    4.0 / 7.0,
                    3.0 / 7.0,
                    2.0 / 7.0,
                    1.0 / 7.0,
                ]
            };
            let (mut aa, mut ab, mut bb, mut ra, mut rb) = (0f32, 0f32, 0f32, 0f32, 0f32);
            for (&k, &v) in best.idx.iter().zip(a.iter()) {
                let wk = w[k as usize];
                if wk < 0.0 {
                    continue;
                }
                let (x, y) = (wk, 1.0 - wk);
                aa += x * x;
                ab += x * y;
                bb += y * y;
                ra += x * v as f32;
                rb += y * v as f32;
            }
            let Some((v0, v1)) = solve_endpoints(aa, ab, bb, ra, rb) else {
                break;
            };
            let n0 = v0.round().clamp(0.0, 255.0) as u8;
            let n1 = v1.round().clamp(0.0, 255.0) as u8;
            let before = best.err;
            // Endpoint order selects the mode: a0 > a1 is 8-level, a0 <= a1 is 6-level.
            if six {
                best.consider(a, n0.min(n1), n0.max(n1));
            } else {
                best.consider(a, n0.max(n1), n0.min(n1));
            }
            if best.err >= before {
                break;
            }
        }
    }
    // Pack the sixteen 3-bit indices into 48 bits, texel 0 in the lowest bits, stored
    // little-endian in bytes 2..8 (SPEC section 6).
    let mut bits = 0u64;
    for (i, &v) in best.idx.iter().enumerate() {
        bits |= (v as u64) << (3 * i);
    }
    let mut out = [0u8; 8];
    out[0] = best.a0;
    out[1] = best.a1;
    out[2..8].copy_from_slice(&bits.to_le_bytes()[..6]);
    out
}

/// Encode one 4x4 block from 16 RGBA texels (row-major) into `out`, which must be exactly
/// 8 (DXT1) or 16 (DXT5) bytes long.
fn encode_block(px: &[[u8; 4]; 16], fmt: Format, out: &mut [u8]) {
    let mut rgb = [[0i32; 3]; 16];
    for i in 0..16 {
        rgb[i] = [px[i][0] as i32, px[i][1] as i32, px[i][2] as i32];
    }
    let (punch, off) = match fmt {
        Format::Dxt1 { punch } => (punch, 0),
        // DXT5 keeps alpha in its own block, so its colour block never needs the transparent
        // 3-colour mode (punch is false); only DXT1 can punch through. The 8 alpha bytes come
        // first, the colour block follows.
        Format::Dxt5 => (false, 8),
    };
    // Without punch-through, alpha is ignored for the colour fit: every texel counts as opaque.
    let mut opaque = [true; 16];
    if punch {
        for i in 0..16 {
            opaque[i] = px[i][3] >= OPAQUE_ALPHA_MIN;
        }
    }
    if fmt == Format::Dxt5 {
        let mut al = [0u8; 16];
        for i in 0..16 {
            al[i] = px[i][3];
        }
        out[..8].copy_from_slice(&fit_alpha(&al));
    }
    let (c0, c1, idx) = fit_colour(&rgb, &opaque);
    out[off..off + 2].copy_from_slice(&c0.to_le_bytes());
    out[off + 2..off + 4].copy_from_slice(&c1.to_le_bytes());
    out[off + 4..off + 8].copy_from_slice(&idx.to_le_bytes());
}

/// Compress a whole RGBA8 image. Preconditions (checked by `encode::encode`): `w` and `h` are
/// multiples of 4 and at least 4, and `rgba` holds `w * h * 4` bytes. A side under 4 holds no
/// whole block, so the result is empty (it never panics on that).
pub(crate) fn encode_image(rgba: &[u8], w: usize, h: usize, fmt: Format) -> Vec<u8> {
    if w < 4 || h < 4 {
        return Vec::new();
    }
    // Bytes per 4x4 block.
    let bb = match fmt {
        Format::Dxt1 { .. } => 8,
        Format::Dxt5 => 16,
    };
    let row_bytes = (w / 4) * bb;
    let mut out = vec![0u8; row_bytes * (h / 4)];
    for (by, row) in out.chunks_mut(row_bytes).enumerate() {
        let mut px = [[0u8; 4]; 16];
        for bx in 0..w / 4 {
            for y in 0..4 {
                let src = ((by * 4 + y) * w + bx * 4) * 4;
                for x in 0..4 {
                    px[y * 4 + x].copy_from_slice(&rgba[src + x * 4..src + x * 4 + 4]);
                }
            }
            encode_block(&px, fmt, &mut row[bx * bb..(bx + 1) * bb]);
        }
    }
    out
}

/// Decode the colour half of a block (8 bytes) to 16 RGBA texels. With `with_alpha_mode` (DXT1),
/// index 3 of a 3-colour block (c0 <= c1) is transparent. DXT5 passes false because its alpha
/// comes from the separate alpha block, and its colour half is always opaque.
fn decode_colour(b: &[u8], with_alpha_mode: bool) -> [[u8; 4]; 16] {
    let c0 = u16::from_le_bytes([b[0], b[1]]);
    let c1 = u16::from_le_bytes([b[2], b[3]]);
    let idx = u32::from_le_bytes([b[4], b[5], b[6], b[7]]);
    let mode3 = c0 <= c1;
    let pal = palette(c0, c1, mode3);
    let mut out = [[0u8; 4]; 16];
    for (i, o) in out.iter_mut().enumerate() {
        let k = ((idx >> (2 * i)) & 3) as usize;
        if mode3 && k == 3 && with_alpha_mode {
            *o = [0, 0, 0, 0];
        } else {
            *o = [pal[k][0] as u8, pal[k][1] as u8, pal[k][2] as u8, 255];
        }
    }
    out
}

/// Decode a DXT1/DXT5 mip to RGBA8. DXT1 c0<=c1 blocks give transparent texels at index 3.
/// `w` and `h` must be nonzero multiples of 4 and `data` exactly the blocks of that size;
/// anything else is an error.
pub fn decode_image(data: &[u8], w: usize, h: usize, dxt5: bool) -> Result<Vec<u8>, Error> {
    let bb = if dxt5 { 16 } else { 8 };
    if w == 0 || h == 0 || !w.is_multiple_of(4) || !h.is_multiple_of(4) {
        return Err(Error::Paa(format!(
            "mip {w}x{h}: sides must be multiples of 4"
        )));
    }
    if w.checked_mul(h).map(|n| n / 16 * bb) != Some(data.len()) {
        return Err(Error::Paa(format!(
            "mip {w}x{h}: data size {} does not match",
            data.len()
        )));
    }
    let mut out = vec![0u8; w * h * 4];
    for by in 0..h / 4 {
        for bx in 0..w / 4 {
            let blk = &data[(by * (w / 4) + bx) * bb..][..bb];
            let mut px = if dxt5 {
                decode_colour(&blk[8..], false)
            } else {
                decode_colour(blk, true)
            };
            if dxt5 {
                let pal = alpha_levels(blk[0], blk[1]);
                let mut raw = [0u8; 8];
                raw[..6].copy_from_slice(&blk[2..8]);
                let bits = u64::from_le_bytes(raw);
                for (i, p) in px.iter_mut().enumerate() {
                    p[3] = pal[((bits >> (3 * i)) & 7) as usize];
                }
            }
            for y in 0..4 {
                for x in 0..4 {
                    let o = ((by * 4 + y) * w + bx * 4 + x) * 4;
                    out[o..o + 4].copy_from_slice(&px[y * 4 + x]);
                }
            }
        }
    }
    Ok(out)
}
