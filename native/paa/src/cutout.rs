//! The 1-bit alpha mask of a DXT1 texture (a "cut-out"): which texels stay opaque.
//!
//! ImageToPAA does not threshold the alpha of a DXT1 mip. It dithers it: in every 4x4 block the opaque texels are the highest-alpha ones
//! (every block measured is monotone in alpha), their number follows the block's summed alpha, and
//! the opaque share of a mip stays near the mean alpha of the mip. The exact rule (which texel flips,
//! and where) depends on the position and the neighbours and was NOT reverse engineered: a ramp gives
//! a regular 8 texel pattern, a flat alpha does not dither at all, and one block seen among different
//! neighbours is masked in up to three ways.
//!
//! What Loki does instead is a deterministic cut-out with the same two measured properties:
//! * a block of one alpha is opaque from alpha 4 up and clear below (ImageToPAA: every flat image
//!   from alpha 4 up came out opaque, 0 to 3 clear; on a palette image with flat patches of alpha 64
//!   27 of 32 such blocks were opaque);
//! * all other blocks of a mip share one threshold, chosen so that the share of their opaque texels
//!   matches their mean alpha (`coverage_cutoff`), closest to 128 where several thresholds give the
//!   same count.
//!
//! On art whose alpha is mostly 0 or 255 (hair cards, foliage, fences) that is a plain threshold at 128
//! at mip 0 (99.65 percent of the texels of a real hair-card texture agree with the oracle) and it keeps the
//! coverage down the chain, where a fixed threshold of 128 lets the small mips fade out.

/// A block of one alpha is opaque from this alpha up. Observed on flat images: 4 and above opaque,
/// 3 and below clear.
const FLAT_OPAQUE_FROM: u8 = 4;

/// Cut-out threshold for a set of texels, given as the histogram of their alpha: texels with alpha
/// `>=` the result are opaque. In `1..=256` (256: nothing is opaque; 0 is never returned, a texel of
/// alpha 0 is always clear).
///
/// The opaque count is the one nearest to `round(sum(alpha) / 255)` among the counts a threshold can
/// give (a tie goes to the larger count); among the thresholds with that count the one nearest to 128 is taken.
pub(crate) fn coverage_cutoff(hist: &[u64; 256]) -> u16 {
    let total: u64 = hist.iter().sum();
    let sum: u64 = hist.iter().enumerate().map(|(a, &n)| a as u64 * n).sum();
    let target = ((sum + 127) / 255).min(total);
    // opaque[t - 1] = number of texels with alpha >= t, for t in 1..=256.
    let mut opaque = [0u64; 256];
    let mut run = 0u64;
    for t in (1..=255usize).rev() {
        run += hist[t];
        opaque[t - 1] = run;
    }
    let best = opaque
        .iter()
        .copied()
        .min_by_key(|&c| (c.abs_diff(target), u64::MAX - c))
        .unwrap_or(0);
    // The thresholds that give that count form one interval: pick the one nearest to 128.
    let (mut lo, mut hi) = (u16::MAX, 0u16);
    for (i, &c) in opaque.iter().enumerate() {
        if c == best {
            lo = lo.min(i as u16 + 1);
            hi = hi.max(i as u16 + 1);
        }
    }
    128u16.clamp(lo, hi)
}

/// True when the 4x4 block at block column `bx`, block row `by` has one alpha on all 16 texels.
fn flat_block(rgba: &[u8], w: usize, bx: usize, by: usize) -> bool {
    let at = |x: usize, y: usize| rgba[((by * 4 + y) * w + bx * 4 + x) * 4 + 3];
    let a0 = at(0, 0);
    (0..4).all(|y| (0..4).all(|x| at(x, y) == a0))
}

/// Set the alpha of every texel of the mip `rgba` (`w` x `h` RGBA bytes, both a multiple of 4) to 0 or
/// 255: flat blocks by `FLAT_OPAQUE_FROM`, the rest by `coverage_cutoff`.
pub(crate) fn apply(rgba: &mut [u8], w: usize, h: usize) {
    let (bw, bh) = (w / 4, h / 4);
    let flat: Vec<bool> = (0..bw * bh)
        .map(|i| flat_block(rgba, w, i % bw, i / bw))
        .collect();
    let mut hist = [0u64; 256];
    for (i, p) in rgba.chunks_exact(4).enumerate() {
        let (x, y) = (i % w, i / w);
        if !flat[(y / 4) * bw + x / 4] {
            hist[p[3] as usize] += 1;
        }
    }
    let t = coverage_cutoff(&hist);
    for (i, p) in rgba.chunks_exact_mut(4).enumerate() {
        let (x, y) = (i % w, i / w);
        let cut = if flat[(y / 4) * bw + x / 4] {
            u16::from(FLAT_OPAQUE_FROM)
        } else {
            t
        };
        p[3] = if u16::from(p[3]) >= cut { 255 } else { 0 };
    }
}
