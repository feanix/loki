//! Golden-output tests: byte-for-byte guard for the encoder.
//!
//! Any change to dxt.rs or encode.rs that is not meant to alter output must leave these hashes
//! unchanged. The test images are synthetic (a fixed pseudo-random generator), so no binary
//! fixtures are stored. Each case encodes one image with one rule and compares an FNV-1a hash of
//! the whole .paa byte stream. If output changes on purpose, re-record the hashes and say why in
//! the commit.
//!
//! The cases cover the main rule families (co, ca, plain, nohq) and the large sizes where the
//! LZO layer applies.

use crate::encode;
use crate::img::Image;
use crate::rules::{self, Rule};

/// The rule of a suffix ("" = unsuffixed).
fn rule(suffix: &str) -> &'static Rule {
    if suffix.is_empty() {
        &rules::PLAIN
    } else {
        rules::find(suffix).unwrap()
    }
}

/// FNV-1a 64 of a byte string.
fn fnv(data: &[u8]) -> u64 {
    data.iter().fold(0xcbf2_9ce4_8422_2325u64, |h, &b| (h ^ b as u64).wrapping_mul(0x0100_0000_01b3))
}

/// Deterministic test image: smooth gradient, noise, flat patches, and an alpha pattern.
/// `alpha`: 0 opaque, 1 binary (0 or 255), 2 soft (random, flat patches stay 255), 3 near-opaque
/// (alpha 245 to 255).
fn synth(w: usize, h: usize, alpha: u8) -> Image {
    // Numerical Recipes LCG (multiplier 1664525, increment 1013904223); the top byte is the
    // least patterned.
    let mut s = 0x1234_5678u32;
    let mut rnd = move || {
        s = s.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        (s >> 24) as u8
    };
    let mut rgba = Vec::with_capacity(w * h * 4);
    for y in 0..h {
        for x in 0..w {
            // 16-pixel checker of flat colour blocks, so the encoder also sees uniform DXT blocks,
            // not only noisy ones.
            let flat = (x / 16 + y / 16) % 5 == 0;
            let (r, g, b) = if flat {
                (200, 90, 30)
            } else {
                ((x * 255 / w) as u8, (y * 255 / h) as u8, (rnd() / 2).wrapping_add(((x ^ y) & 63) as u8))
            };
            let a = match alpha {
                0 => 255,
                1 => {
                    if (x / 8 + y / 8) % 3 == 0 {
                        0
                    } else {
                        255
                    }
                }
                2 => {
                    if flat {
                        255
                    } else {
                        rnd()
                    }
                }
                _ => 245 + rnd() % 11,
            };
            rgba.extend_from_slice(&[r, g, b, a]);
        }
    }
    Image { w, h, rgba }
}

#[test]
fn golden_hashes() {
    // co, ca and plain average their mips in linear light, weighted by alpha, to match
    // ImageToPAA. Mip 0 is unaffected by this.
    // Fields: (case name, rule suffix, width, height, alpha mode for `synth`, expected FNV-1a hash
    // of the encoded file).
    let cases: &[(&str, &str, usize, usize, u8, u64)] = &[
        ("co_opaque", "co", 64, 64, 0, 0x88202944b1d9b0f5),
        ("co_binary", "co", 64, 32, 1, 0x2e73a7bb25eebd2e),
        ("co_soft", "co", 32, 64, 2, 0x62c67a30a459d61e),
        ("ca_opaque", "ca", 64, 64, 0, 0xef33eb4cb092cf12),
        ("ca_soft", "ca", 64, 64, 2, 0x79ca463546e51261),
        ("ca_near_opaque", "ca", 64, 64, 3, 0xc6b7ef1761d952b0),
        ("plain_opaque", "", 64, 64, 0, 0x88202944b1d9b0f5),
        ("plain_binary", "", 64, 64, 1, 0x13006a28d2c53998),
        ("plain_soft", "", 64, 64, 2, 0x79ca463546e51261),
        // nohq: the normal-map alpha chain runs first, every level (including level 0) is
        // renormalised, the swizzle comes after the chain, and the FLAG tag is set from alpha.
        // R and G are truncated in the chain.
        ("nohq_opaque", "nohq", 64, 64, 0, 0xb47c7caeeb59fcb1),
        ("nohq_soft", "nohq", 64, 32, 2, 0xcda6b76bb1faeb5e),
        // 256 wide: wide enough for the LZO layer to apply to the top mips (see `LZO_MIN_WIDTH`
        // in encode.rs).
        ("co_lzo", "co", 256, 128, 0, 0x25f199e715a2a93f),
        ("ca_lzo", "ca", 256, 256, 2, 0xefa2503196bbecf3),
    ];
    let mut bad = Vec::new();
    for &(name, suffix, w, h, alpha, want) in cases {
        let got = fnv(&encode::encode(synth(w, h, alpha), rule(suffix)).unwrap());
        if got != want {
            bad.push(format!("{name}: got 0x{got:016x}, want 0x{want:016x}"));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}
