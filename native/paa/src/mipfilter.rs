//! The per-class `mipmapFilter` behaviours, fitted to ImageToPAA output.
//!
//! A mip chain has two parts: the CHAIN (level k+1 from the 8-bit level k) and the OUTPUT shaping
//! applied to each level when it is stored. Observed on ARGB test files (every texel of mips 0..3
//! compared, 99.8..100% equal) and on DXT files whose mips are uniform per tile:
//! * The byte box of every chain level truncates R and G and rounds B and A (`halve_chain`; it
//!   made `_raw`, `_nofex` and `_nsex` bit-identical to ImageToPAA and removed a mean drift of
//!   about 0.4 per level in `_nohq` and `_as`).
//! * Normal-map filters: the chain is a 2x2 byte average (`NormalizeNormalMap`,
//!   `NormalizeNormalMapFade`) or the same average weighted by alpha (`NormalizeNormalMapAlpha`,
//!   colour only; alpha stays the plain mean). Both use the byte box rounding above: R and G
//!   truncate, B and A round. Each level, mip 0 included, is stored renormalised: vectors
//!   `c/255*2-1` scaled to unit length and re-encoded with rounding. The renormalised level is
//!   not fed back into the chain. `NormalizeNormalMapNoise` is handled like `NormalizeNormalMap`
//!   here, but `encode` refuses rules that use it (`Rule::unreplicable`), so its noise is never
//!   produced.
//! * `NormalizeNormalMapFade` and `FadeOut` move the level toward flat by an amount that depends
//!   on the mip's longer side (see `fade_amount`). Observed on 512x128, 128x512 and 1024x128
//!   DXT: a 64x16 mip fades like 64x64. The averaged vector moves toward (0, 0, 1) and is then
//!   renormalised, or every channel (alpha included) moves toward 128. This also applies to
//!   mip 0 of a small texture.
//! * `Rule::linear_mips` (observed for `dynRange` 1 in ImageToPAA output): colour is averaged in
//!   linear light, weighted by alpha, and converted back to sRGB bytes; alpha is the plain box
//!   mean. Fits 100% of the texels of mip 1 on opaque images and 99.8% on a soft-alpha ramp.
//!
//! Everything is `f64`; the only libm calls are `sqrt` and `powf` (in the sRGB curves), so the
//! output is the same on every machine except for a possible last-bit difference in `powf`.

use crate::img::Image;
use crate::rules::Filter;
use std::sync::OnceLock;

/// How far a level of this longer side is faded toward the flat value, 0 (none) to 1 (fully).
/// Observed (ARGB `_nofex`, every texel matches): 64: 1/5, 32: 1/4, 16: 0.33, 8: 1/2, 4: 3/4 and
/// 1: 1; 128 and up: none (DXT files). Side 2 was not measured exactly; 0.875 is a point inside
/// the observed range 0.80 to 0.88.
pub(crate) fn fade_amount(side: usize) -> f64 {
    match side {
        128.. => 0.0,
        64.. => 0.2,
        32.. => 0.25,
        16.. => 0.33,
        8.. => 0.5,
        4.. => 0.75,
        2.. => 0.875,
        _ => 1.0,
    }
}

/// Map a unit-vector component in -1..1 to a byte 0..255 (the usual normal-map encoding),
/// rounding to nearest.
fn encode_unit(v: f64) -> u8 {
    ((v * 0.5 + 0.5) * 255.0).round().clamp(0.0, 255.0) as u8
}

/// Normalise `m`; a zero vector becomes the flat normal (0, 0, 1).
fn normalised(m: [f64; 3]) -> [f64; 3] {
    let len = (m[0] * m[0] + m[1] * m[1] + m[2] * m[2]).sqrt();
    if len < 1e-9 {
        [0.0, 0.0, 1.0]
    } else {
        [m[0] / len, m[1] / len, m[2] / len]
    }
}

/// True for the four filters that treat the texture as a tangent-space normal map.
fn is_normal_filter(filter: Option<Filter>) -> bool {
    matches!(
        filter,
        Some(Filter::NormalizeNormalMap | Filter::NormalizeNormalMapAlpha | Filter::NormalizeNormalMapFade | Filter::NormalizeNormalMapNoise)
    )
}

/// sRGB byte to linear light, by table.
fn to_linear(v: u8) -> f64 {
    static T: OnceLock<[f64; 256]> = OnceLock::new();
    T.get_or_init(|| {
        let mut t = [0f64; 256];
        for (i, e) in t.iter_mut().enumerate() {
            let c = i as f64 / 255.0;
            *e = if c <= 0.04045 { c / 12.92 } else { ((c + 0.055) / 1.055).powf(2.4) };
        }
        t
    })[v as usize]
}

/// Linear light to an sRGB byte (inverse of `to_linear`), rounded and clamped.
fn from_linear(l: f64) -> u8 {
    let c = if l <= 0.0031308 { l * 12.92 } else { 1.055 * l.powf(1.0 / 2.4) - 0.055 };
    (c * 255.0).round().clamp(0.0, 255.0) as u8
}

/// The four source texels of output texel (x, y) of the half-size level.
fn quad(img: &Image, x: usize, y: usize) -> [[u8; 4]; 4] {
    let mut px = [[0u8; 4]; 4];
    for (i, (dx, dy)) in [(0, 0), (1, 0), (0, 1), (1, 1)].into_iter().enumerate() {
        let o = ((2 * y + dy) * img.w + 2 * x + dx) * 4;
        px[i].copy_from_slice(&img.rgba[o..o + 4]);
    }
    px
}

/// Halve with colour weighted by alpha. `linear`: average in linear light, else in the bytes.
/// Equal weights when all four texels are transparent; alpha is the plain box mean.
fn halve_weighted(img: &Image, linear: bool) -> Image {
    let (w, h) = (img.w / 2, img.h / 2);
    let mut rgba = vec![0u8; w * h * 4];
    for y in 0..h {
        for x in 0..w {
            let px = quad(img, x, y);
            let wsum: f64 = px.iter().map(|p| p[3] as f64).sum();
            // Fully transparent quad: no weights, so fall back to a plain average (1/4 each).
            let wt = |p: &[u8; 4]| if wsum > 0.0 { p[3] as f64 / wsum } else { 0.25 };
            let d = &mut rgba[(y * w + x) * 4..][..4];
            for c in 0..3 {
                d[c] = if linear {
                    from_linear(px.iter().map(|p| to_linear(p[c]) * wt(p)).sum())
                } else {
                    // R and G truncate, B rounds (see `halve_chain`)
                    (px.iter().map(|p| p[c] as f64 * wt(p)).sum::<f64>() + if c < 2 { 0.0 } else { 0.5 }).floor().clamp(0.0, 255.0) as u8
                };
            }
            // +2 makes the integer divide round to nearest, matching the tool's alpha.
            d[3] = ((px.iter().map(|p| p[3] as u32).sum::<u32>() + 2) / 4) as u8;
        }
    }
    Image { w, h, rgba }
}

/// The oracle's byte box for a mip chain: the mean of the four texels truncated for R and G and
/// rounded for B and A. Observed on `_raw` ARGB1555 files: exact on every texel of mips 1 to 3
/// of a photo-like image, with R and G 100% floor and B 100% round; the truncation also shows as a mean
/// that drifts down by about 0.4 per level in R and G in DXT mips of real art. Not used by
/// `limit_size`, which is Loki's own resize and not an ImageToPAA behaviour.
fn halve_chain(img: &Image) -> Image {
    let (w, h) = (img.w / 2, img.h / 2);
    let mut rgba = vec![0u8; w * h * 4];
    for y in 0..h {
        for x in 0..w {
            let s = |dx: usize, dy: usize, c: usize| img.rgba[((2 * y + dy) * img.w + 2 * x + dx) * 4 + c] as u32;
            for c in 0..4 {
                let sum = s(0, 0, c) + s(1, 0, c) + s(0, 1, c) + s(1, 1, c);
                rgba[(y * w + x) * 4 + c] = (if c < 2 { sum / 4 } else { (sum + 2) / 4 }) as u8;
            }
        }
    }
    Image { w, h, rgba }
}

/// The next (half size) level of the chain for `filter`; `linear` selects the linear-light
/// average for the plain (non-normal-map) case.
pub(crate) fn next_mip(img: &Image, filter: Option<Filter>, linear: bool) -> Image {
    match filter {
        Some(Filter::NormalizeNormalMapAlpha) => halve_weighted(img, false),
        _ if is_normal_filter(filter) => halve_chain(img),
        _ if linear => halve_weighted(img, true),
        _ => halve_chain(img),
    }
}

/// True when `filter` changes the stored level (normal maps are renormalised, `FadeOut` fades).
pub(crate) fn shapes_output(filter: Option<Filter>) -> bool {
    is_normal_filter(filter) || filter == Some(Filter::FadeOut)
}

/// Shape a level for storing: renormalise normal maps (`NormalizeNormalMapFade` first moves the
/// averaged vector toward the flat normal (0, 0, 1) by `fade_amount`) or, for `FadeOut`, move every
/// channel toward 128 by it. Other filters leave the level alone.
pub(crate) fn output(rgba: &mut [u8], filter: Option<Filter>, w: usize, h: usize) {
    let t = if matches!(filter, Some(Filter::NormalizeNormalMapFade | Filter::FadeOut)) { fade_amount(w.max(h)) } else { 0.0 };
    if is_normal_filter(filter) {
        // Lerp the decoded vector toward the flat normal (0, 0, 1) by t, then renormalise and
        // re-encode. With t = 0 this is a plain renormalise.
        for p in rgba.chunks_exact_mut(4) {
            let v = [p[0], p[1], p[2]].map(|c| c as f64 / 255.0 * 2.0 - 1.0);
            let n = normalised([v[0] * (1.0 - t), v[1] * (1.0 - t), v[2] * (1.0 - t) + t]);
            for c in 0..3 {
                p[c] = encode_unit(n[c]);
            }
        }
    } else if filter == Some(Filter::FadeOut) && t > 0.0 {
        // FadeOut: lerp every byte toward 128 by t; t = 0 means no change, so skip.
        for v in rgba.iter_mut() {
            *v = (*v as f64 + (128.0 - *v as f64) * t).round().clamp(0.0, 255.0) as u8;
        }
    }
}
