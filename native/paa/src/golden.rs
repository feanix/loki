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
    data.iter().fold(0xcbf2_9ce4_8422_2325u64, |h, &b| {
        (h ^ b as u64).wrapping_mul(0x0100_0000_01b3)
    })
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
                (
                    (x * 255 / w) as u8,
                    (y * 255 / h) as u8,
                    (rnd() / 2).wrapping_add(((x ^ y) & 63) as u8),
                )
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
    // Two hash pairs are equal on purpose, not a copy error: `plain_opaque` equals `co_opaque` and
    // `plain_soft` equals `ca_soft`, because the unsuffixed rule and `_co`/`_ca` store the same bytes
    // for those inputs (same format, FLAG and mip filter). The pairs are asserted after the loop
    // below, so a change that makes one class drift from the other fails by name.
    // ca_soft, plain_soft, ca_lzo, nohq_opaque, nohq_256_soft and nohq_512 (DXT5 cases whose alpha blocks
    // changed; ca_near_opaque and nohq_soft came out the same) were re-recorded for the DXT5 alpha block fit (all endpoint pairs among the block's own values, a local
    // search and least squares, run only on blocks with a poor first fit and few distinct values: lower alpha error,
    // so different alpha blocks); the DXT1 cases are unchanged.
    // co_soft, ca_soft, plain_soft, ca_near_opaque and ca_lzo were earlier re-recorded for the AVGC tag
    // only (the per-texel colour table of `avgc.rs`). Checked once against the build before the AVGC change on a
    // near-opaque image: the files differ only in the three AVGC colour bytes. co_soft and plain_binary
    // were re-recorded again for the DXT1 1-bit alpha masks (`cutout.rs`: the masks of mips 1 down follow the
    // coverage of the level; mip 0 of a binary alpha is unchanged).
    let cases: &[(&str, &str, usize, usize, u8, u64)] = &[
        ("co_opaque", "co", 64, 64, 0, 0x88202944b1d9b0f5),
        ("co_binary", "co", 64, 32, 1, 0x2e73a7bb25eebd2e),
        ("co_soft", "co", 32, 64, 2, 0x89acd0b9dfc92589),
        ("ca_opaque", "ca", 64, 64, 0, 0xef33eb4cb092cf12),
        ("ca_soft", "ca", 64, 64, 2, 0x3c1be8123b4afcc5),
        ("ca_near_opaque", "ca", 64, 64, 3, 0x9577ae99139ff92e),
        ("plain_opaque", "", 64, 64, 0, 0x88202944b1d9b0f5),
        ("plain_binary", "", 64, 64, 1, 0x504b5377a78f266f),
        ("plain_soft", "", 64, 64, 2, 0x3c1be8123b4afcc5),
        // nohq: the normal-map alpha chain runs first, every level (including level 0) is
        // renormalised, the swizzle comes after the chain, and the FLAG tag is set from alpha.
        // R and G are truncated in the box chain. nohq_soft changed with the fitted rounding: the alpha-weighted mean
        // is now rounded half to even (was float weights with a floor), so every partly transparent quad can differ.
        ("nohq_opaque", "nohq", 64, 64, 0, 0x886240f565f82891),
        ("nohq_soft", "nohq", 64, 32, 2, 0x66a114d0bb9e7e89),
        // 256 wide: wide enough for the LZO layer to apply to the top mips (see `LZO_MIN_WIDTH`
        // in encode.rs).
        ("co_lzo", "co", 256, 128, 0, 0x25f199e715a2a93f),
        ("ca_lzo", "ca", 256, 256, 2, 0x5d0860f418fcf5b7),
    ];
    let mut bad = Vec::new();
    for &(name, suffix, w, h, alpha, want) in cases {
        let got = fnv(&encode::encode(synth(w, h, alpha), rule(suffix)).unwrap());
        if got != want {
            bad.push(format!("{name}: got 0x{got:016x}, want 0x{want:016x}"));
        }
    }
    // The two intentional duplicates (see above): the table must keep each pair equal.
    let hash_of = |n: &str| cases.iter().find(|c| c.0 == n).unwrap().5;
    for (a, b) in [("co_opaque", "plain_opaque"), ("ca_soft", "plain_soft")] {
        if hash_of(a) != hash_of(b) {
            bad.push(format!("{a} and {b} must have the same hash"));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

// Detail (`dt`) and normal-map (`nohq`) classes at the two sizes that matter for them: 256 (LZO
// applies to the top mips) and 512 (one more chain level). The first golden table has `nohq` only at
// 64, where the whole chain is stored raw. Recorded from the encoder as it was then; nohq_256_soft re-recorded for the fitted rounding (alpha-weighted rounding, soft alpha).
#[test]
fn golden_hashes_detail_and_normal_maps() {
    let cases: &[(&str, &str, usize, usize, u8, u64)] = &[
        ("dt_256", "dt", 256, 256, 0, 0x5df3eca774d41b71),
        ("dt_512_binary", "dt", 512, 512, 1, 0x068052eed038606a),
        ("nohq_256_soft", "nohq", 256, 256, 2, 0x5cf7cac8e2c069d9),
        ("nohq_512", "nohq", 512, 512, 0, 0xe8622be13aff1b54),
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

// DXT1 punch-through over many kinds of block: all transparent, solid colour with a few holes,
// solid with a single opaque texel, and a two-colour gradient with holes. Alpha must come back
// exactly as the 128 cut-off says (transparent texels 0, opaque 255) in every block, and the bytes are
// pinned. `Dxt1 { punch: true }` is reachable only through this direct call: `encode_file` refuses
// 1-bit alpha for DXT1, so no other test reaches the 3-colour block path with holes.
#[test]
fn dxt1_punch_through_over_many_block_kinds() {
    use crate::dxt::{decode_image, encode_image, Format};
    let (w, h) = (64usize, 64usize);
    let mut s = 0x0bad_cafeu32;
    let mut rnd = move || {
        s = s.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        (s >> 24) as u8
    };
    let mut rgba = Vec::with_capacity(w * h * 4);
    let mut kind_of = vec![0u8; w * h];
    for y in 0..h {
        for x in 0..w {
            let kind = ((x / 4) + (y / 4) * 3 + (x / 4) / 5) % 4;
            kind_of[y * w + x] = kind as u8;
            let px = match kind {
                0 => [rnd(), rnd(), rnd(), 0], // all transparent
                1 => [30, 160, 220, if rnd() < 64 { 0 } else { 255 }], // a few holes
                2 => [30, 160, 220, if (x % 4, y % 4) == (1, 2) { 255 } else { 0 }], // one opaque texel
                _ => [
                    (x * 4) as u8,
                    (y * 4) as u8,
                    90,
                    if rnd() < 90 { 0 } else { 255 },
                ], // gradient, holes
            };
            rgba.extend_from_slice(&px);
        }
    }
    let data = encode_image(&rgba, w, h, Format::Dxt1 { punch: true });
    let out = decode_image(&data, w, h, false).unwrap();
    for i in 0..w * h {
        let (src, got) = (&rgba[i * 4..][..4], &out[i * 4..][..4]);
        assert_eq!(
            got[3],
            if src[3] >= 128 { 255 } else { 0 },
            "texel {i} (block kind {})",
            kind_of[i]
        );
        if (kind_of[i] == 1 || kind_of[i] == 2) && src[3] >= 128 {
            // solid colour blocks come back within the 5/6-bit quantisation step
            assert!(
                src[..3].iter().zip(got).all(|(&a, &b)| a.abs_diff(b) <= 8),
                "texel {i}: {src:?} -> {got:?}"
            );
        }
    }
    assert_eq!(fnv(&data), 0x704a_5965_6799_7947, "punch-through blocks");
}
