//! Tests for soft alpha: the AVGC colour of texels with alpha, and the DXT1 1-bit alpha
//! mask. The numbers are ImageToPAA observations, not outputs of
//! this encoder.

use crate::encode;
use crate::img::Image;
use crate::paa;
use crate::tests::rule;

/// AVGC tag bytes (B, G, R, A) of an encode of `img` by the rule of `suffix`.
fn avgc(img: Image, suffix: &str) -> Vec<u8> {
    let p = paa::read(&encode::encode(img, rule(suffix)).unwrap()).unwrap();
    assert_eq!(p.tags[0].name, "AVGC");
    p.tags[0].data.clone()
}

/// A 16x16 image of one grey and one alpha.
fn flat(c: u8, a: u8) -> Image {
    Image {
        w: 16,
        h: 16,
        rgba: [c, c, c, a].repeat(256),
    }
}

// ImageToPAA, flat grey `c` at alpha `a`, class `_ca` (the same in `_co`, the unsuffixed rule, `_8888`,
// `_gs`, at 4x4 up to 256x256): the stored colour is not `c`. The first row is the datum of the
// task (200 at 128 gives 138); the last rows are the floor near black and the transparent corner.
#[test]
fn flat_soft_alpha_avgc_matches_the_oracle() {
    let cases: &[(u8, u8, u8)] = &[
        (200, 128, 138),
        (200, 64, 100),
        (200, 192, 171),
        (200, 32, 79),
        (255, 128, 175),
        (255, 64, 125),
        (255, 32, 95),
        (255, 1, 137),
        (128, 128, 90),
        (128, 192, 110),
        (128, 64, 68),
        (50, 128, 38),
        (0, 128, 4),
        (0, 64, 11),
        (0, 1, 127),
        (0, 255, 1),
        (255, 255, 255),
        (255, 254, 254),
        (0, 0, 0),
    ];
    for &(c, a, want) in cases {
        let got = avgc(flat(c, a), "ca");
        assert_eq!(got, [want, want, want, a], "grey {c} at alpha {a}");
    }
}

// The mean over texels is alpha weighted in linear light of the stored colour (observed on mixes of two
// texel kinds, half and half): opaque white with half-transparent black is 213, not the mean of 255 and 4.
#[test]
fn mixed_texels_average_alpha_weighted_in_linear_light() {
    let mix = |p: [u8; 4], q: [u8; 4]| {
        let rgba = (0..256).flat_map(|i| if i < 128 { p } else { q }).collect();
        avgc(Image { w: 16, h: 16, rgba }, "ca")
    };
    assert_eq!(
        mix([255, 255, 255, 255], [0, 0, 0, 128]),
        [213, 213, 213, 192]
    );
    assert_eq!(
        mix([255, 255, 255, 255], [255, 255, 255, 128]),
        [232, 232, 232, 192]
    );
    // R and B: white and black at alpha 128 mix to 128; G: grey 128 at alpha 128 is 90 whatever the mix.
    assert_eq!(
        mix([255, 128, 0, 128], [0, 128, 255, 128])[..3],
        [128, 90, 128]
    );
    // A fully transparent texel has no weight: it neither darkens nor tints the mean.
    assert_eq!(
        mix([255, 255, 255, 255], [255, 255, 255, 0]),
        [255, 255, 255, 128]
    );
}

// The colour channels are independent: grey 200 at alpha 128 in R, 100 in G, 50 in B was seen as 138, 71, 38.
#[test]
fn soft_alpha_avgc_channels_are_independent() {
    let img = Image {
        w: 8,
        h: 8,
        rgba: [200, 100, 50, 128].repeat(64),
    };
    assert_eq!(avgc(img, "ca"), [38, 71, 138, 128]);
}

// The data classes (`dynRange` 0) keep the plain alpha weighted mean: no stored-colour table there.
#[test]
fn data_classes_keep_the_plain_mean() {
    let img = Image {
        w: 8,
        h: 8,
        rgba: [200, 100, 50, 128].repeat(64),
    };
    assert_eq!(avgc(img, "raw"), [50, 100, 200, 128]);
}

// --- the DXT1 1-bit alpha mask (cutout.rs) ----------------------------------------------------------

use crate::cutout;

/// Histogram of alpha values.
fn hist_of(alphas: &[u8]) -> [u64; 256] {
    let mut h = [0u64; 256];
    for &a in alphas {
        h[a as usize] += 1;
    }
    h
}

/// A `w` x `h` RGBA mip (grey texels) of `alphas`, and its alpha after `cutout::apply`.
fn cut(alphas: &[u8], w: usize, h: usize) -> Vec<u8> {
    let mut px: Vec<u8> = alphas.iter().flat_map(|&a| [90, 90, 90, a]).collect();
    cutout::apply(&mut px, w, h);
    px.chunks_exact(4).map(|p| p[3]).collect()
}

// Binary alpha (0 or 255, the usual cut-out art) keeps the plain threshold at 128: ImageToPAA's mask agrees
// with a threshold on 99.6 percent of the texels of a real hair-card texture.
#[test]
fn binary_alpha_is_cut_at_128() {
    let alphas: Vec<u8> = (0..64).map(|i| if i % 3 == 0 { 0 } else { 255 }).collect();
    assert_eq!(cutout::coverage_cutoff(&hist_of(&alphas)), 128);
    assert_eq!(cut(&alphas, 8, 8), alphas);
}

// A soft edge takes the threshold that keeps the opaque share at the mean alpha: a ramp of 0..=255
// (mean alpha 127.5, so 128 of 256 texels) is cut at 128; the same ramp with a faint tail (alpha <= 100
// for most texels, mean 55) is cut lower, so a feathered edge keeps its coverage.
#[test]
fn threshold_follows_the_mean_alpha() {
    let ramp: Vec<u8> = (0..=255).collect();
    assert_eq!(cutout::coverage_cutoff(&hist_of(&ramp)), 128);
    let faint: Vec<u8> = (0..256u32).map(|i| (i * 100 / 255) as u8).collect();
    let t = cutout::coverage_cutoff(&hist_of(&faint));
    assert!(t < 128 && t > 1, "threshold {t}");
    let opaque = faint.iter().filter(|&&a| u16::from(a) >= t).count() as f64;
    let mean = faint.iter().map(|&a| a as f64).sum::<f64>() / 255.0;
    assert!(
        (opaque - mean).abs() <= 1.0,
        "opaque {opaque}, mean alpha {mean}"
    );
}

// A texel of alpha 0 is never opaque; blocks of one alpha are opaque from alpha 4 (ImageToPAA: every flat
// image from 4 up came out opaque, 0 to 3 clear), whatever the mean alpha of the mip.
#[test]
fn alpha_zero_stays_clear_and_flat_blocks_are_opaque_from_4() {
    assert_eq!(
        cut(&[0, 0, 0, 0, 255, 255, 255, 255].repeat(2), 4, 4),
        [0, 0, 0, 0, 255, 255, 255, 255].repeat(2)
    );
    for (a, want) in [(0u8, 0u8), (3, 0), (4, 255), (60, 255), (200, 255)] {
        assert_eq!(cut(&[a; 16], 4, 4), [want; 16], "flat alpha {a}");
    }
    // A flat block beside a binary one: the flat block (alpha 60) is opaque and does not enter the count of the
    // other block (columns 4..8: 0, 0, 255, 255 on every row).
    let rows: Vec<u8> = (0..4)
        .flat_map(|_| [60, 60, 60, 60, 0, 0, 255, 255])
        .collect();
    let got = cut(&rows, 8, 4);
    assert!(got
        .chunks(8)
        .all(|r| r == [255, 255, 255, 255, 0, 0, 255, 255]));
}

// The point of the per-mip threshold: a 35 percent opaque cut-out keeps about that share down the whole chain.
// A fixed threshold of 128 lets the mean alpha 90 of the 4x4 mip fall below it and clear the whole mip.
#[test]
fn dxt1_cutout_keeps_its_coverage_down_the_chain() {
    let n = 64;
    let rgba: Vec<u8> = (0..n * n)
        .flat_map(|i| {
            [
                200,
                100,
                50,
                if (i % n / 8 + i / n / 8) % 3 == 0 {
                    255
                } else {
                    0
                },
            ]
        })
        .collect();
    let b = encode::encode(Image { w: n, h: n, rgba }, rule("co")).unwrap();
    let p = paa::read(&b).unwrap();
    assert_eq!(p.ty, paa::TYPE_DXT1);
    let last = p.mips.last().unwrap();
    let px = crate::decode_mip(p.ty, &last.data, last.w as usize, last.h as usize).unwrap();
    let opaque = px.chunks_exact(4).filter(|q| q[3] > 0).count();
    assert!(
        opaque >= 3,
        "the 4x4 mip kept {opaque} of 16 texels (a third of the art is opaque)"
    );
}
