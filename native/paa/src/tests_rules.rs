//! Per-suffix tests for the rule table: one case per texture class (the suffix after the
//! underscore in a texture file name, such as `_co` or `_nohq`).
//!
//! ImageToPAA is Bohemia's own converter; it is the reference ("oracle") this crate is compared
//! against. Each rule is one texture class, chosen by file-name suffix, with its own PAA type,
//! FLAG, SWIZ and mip count.
//!
//! The expected structure (type word, FLAG, SWIZ payload, mip count) in `STRUCTURE` was read
//! from ImageToPAA 2.9.2014 output on synthetic 64x64 images (an opaque one, one with a soft
//! alpha ramp, one with 0/255 alpha); the table is facts about its output, not Bohemia's file.
//! A separate oracle comparison on realistic inputs reports PSNR per mip; these tests pin
//! what this crate does so a change shows up.

use crate::argb;
use crate::encode::{self, Layout};
use crate::img::Image;
use crate::mipfilter;
use crate::paa;
use crate::rules::{self, Filter, Fmt, Rule, RULES};

/// Builds an n x n test image, like the ones ImageToPAA was probed with. `alpha` picks the
/// alpha pattern: 0 opaque, 1 soft ramp, 2 hard 0/255 blocks. Red and green are gradients and
/// blue is repeatable noise, so no mip level is flat.
fn probe_image(alpha: usize, n: usize) -> Image {
    let mut s = 7u32;
    let mut rgba = Vec::with_capacity(n * n * 4);
    for y in 0..n {
        for x in 0..n {
            // Tiny linear congruential generator (constants from Numerical Recipes): repeatable
            // noise without a rand dependency.
            s = s.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let a = match alpha {
                0 => 255,
                1 => (255 * x / n) as u8,
                _ => {
                    if (x / 8 + y / 8) % 3 == 0 {
                        0
                    } else {
                        255
                    }
                }
            };
            rgba.extend_from_slice(&[(x * 255 / n) as u8, (y * 255 / n) as u8, (s >> 24) as u8, a]);
        }
    }
    Image { w: n, h: n, rgba }
}

/// Looks up the rule for a suffix, panicking if the table has none.
fn rule(suffix: &str) -> &'static Rule {
    rules::find(suffix).unwrap_or_else(|| panic!("no class for _{suffix}"))
}

/// Reads a PAA and returns (type word, FLAG value if present, SWIZ bytes as hex or empty,
/// mip count).
fn header(bytes: &[u8]) -> (u16, Option<u32>, String, usize) {
    let p = paa::read(bytes).unwrap();
    let tag = |n: &str| p.tags.iter().find(|t| t.name == n).map(|t| t.data.clone());
    let flag = tag("FLAG").map(|d| u32::from_le_bytes([d[0], d[1], d[2], d[3]]));
    let swiz = tag("SWIZ")
        .map(|d| d.iter().map(|b| format!("{b:02x}")).collect::<String>())
        .unwrap_or_default();
    (p.ty, flag, swiz, p.mips.len())
}

/// (suffix, SWIZ hex or "", [(type, FLAG) for opaque, soft, 0/255 alpha]) from ImageToPAA.
/// Type words: 0xFF01 = DXT1, 0xFF05 = DXT5, 0x1555 and 0x4444 = 16-bit ARGB, 0x8080 = AI88
/// (16-bit grey plus alpha). FLAG is the alpha tag and its value depends on the format: 0 means
/// no tag (opaque); DXT5 writes 1 for any real alpha; DXT1 and ARGB1555 write 2; ARGB4444 and
/// AI88 write 1 for soft alpha and 2 for 0/255 alpha.
const STRUCTURE: &[(&str, &str, [(u16, u32); 3])] = &[
    ("8888", "", [(0x1555, 0), (0x1555, 2), (0x1555, 2)]),
    ("co", "", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("raw", "", [(0x1555, 0), (0x1555, 2), (0x1555, 2)]),
    ("ca", "", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("cat", "", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("sky", "06010403", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    (
        "detail",
        "08000000",
        [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)],
    ),
    ("cdt", "", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("lco", "", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("mco", "", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("draftlco", "", [(0x1555, 0), (0x4444, 1), (0x1555, 2)]),
    ("lca", "", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("mask", "", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("pr", "", [(0xFF01, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("ads", "08080203", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("adshq", "02090309", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    (
        "dtsmdi",
        "08010203",
        [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)],
    ),
    ("mc", "", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("as", "08080208", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("sm", "08010203", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("smdi", "08080203", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("dt", "08000000", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("normalmap", "", [(0xFF01, 0), (0xFF05, 1), (0xFF01, 2)]),
    ("no", "08010203", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("noex", "", [(0x1555, 0), (0x4444, 1), (0x1555, 2)]),
    ("non", "", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("nohq", "05040203", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("novhq", "05080208", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("nofhq", "05040203", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("nof", "", [(0xFF01, 0), (0xFF05, 1), (0xFF01, 2)]),
    ("nopx", "05000203", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("nofex", "", [(0x1555, 0), (0x4444, 1), (0x1555, 2)]),
    ("ns", "", [(0xFF01, 0), (0xFF05, 1), (0xFF01, 2)]),
    ("nsex", "", [(0x1555, 0), (0x4444, 1), (0x1555, 2)]),
    ("nshq", "05040203", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("gs", "", [(0x8080, 0), (0x8080, 1), (0x8080, 2)]),
    ("can", "", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
    ("4444", "", [(0x4444, 0), (0x4444, 1), (0x4444, 2)]),
    ("1555", "", [(0x1555, 0), (0x1555, 2), (0x1555, 2)]),
    ("88", "", [(0x8080, 0), (0x8080, 1), (0x8080, 2)]),
    ("dxt1", "", [(0xFF01, 0), (0xFF01, 2), (0xFF01, 2)]),
    ("dxt5", "", [(0xFF05, 0), (0xFF05, 1), (0xFF05, 1)]),
];

#[test]
fn every_class_has_exactly_one_structure_row() {
    // 42 is the number of texture classes in the 2.9.2014 cfg.
    assert_eq!(RULES.len(), 42);
    assert_eq!(STRUCTURE.len(), RULES.len());
    for r in RULES {
        assert_eq!(
            STRUCTURE.iter().filter(|s| s.0 == r.suffix()).count(),
            1,
            "{}",
            r.class
        );
        assert_eq!(rules::find(r.suffix()).unwrap().class, r.class);
        assert_eq!(
            rules::find(&r.suffix().to_uppercase()).unwrap().class,
            r.class
        );
    }
}

#[test]
fn structure_equals_imagetopaa_for_every_suffix() {
    let mut bad = Vec::new();
    for &(suffix, swiz, cells) in STRUCTURE {
        let r = rule(suffix);
        if r.unreplicable().is_some() {
            continue;
        }
        for (alpha, (ty, flag)) in cells.into_iter().enumerate() {
            let bytes = encode::encode(probe_image(alpha, 64), r).unwrap();
            let got = header(&bytes);
            let pixel = matches!(ty, 0x1555 | 0x4444 | 0x8080);
            // A 64x64 pixel-format chain runs 64..1 (7 levels); a DXT chain stops at 4x4 (5
            // levels), since DXT blocks are 4x4.
            let want = (
                ty,
                (flag != 0).then_some(flag),
                swiz.to_string(),
                if pixel { 7 } else { 5 },
            );
            if got != want {
                bad.push(format!(
                    "_{suffix} alpha{alpha}: got {got:?}, want {want:?}"
                ));
            }
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

/// Suffixes that still go to ImageToPAA because Loki cannot write them: none. The noise filters (`_cat`, `_can`,
/// `_non`) are encoded with Loki's own seeded noise, checked against ImageToPAA by statistics (`noise`).
const ROUTED_TO_IMAGETOPAA: [&str; 0] = [];

#[test]
fn every_suffix_is_verified_and_routed_to_loki() {
    for r in RULES {
        let routed = ROUTED_TO_IMAGETOPAA.contains(&r.suffix());
        assert_eq!(r.verified, !routed, "{}", r.class);
        assert!(r.unreplicable().is_none(), "{}", r.class);
        assert_eq!(
            crate::supported(&format!("x_{}.png", r.suffix())),
            !routed,
            "{}",
            r.class
        );
    }
    assert!(rules::PLAIN.verified);
    for s in ["cat", "can", "non"] {
        let bytes = encode::encode(probe_image(1, 16), rule(s)).unwrap();
        assert!(crate::paa::read(&bytes).is_ok(), "_{s}");
    }
}

#[test]
fn table_text_lists_every_class_and_key() {
    let t = rules::table_text();
    assert!(t.contains("convertVersion=6"));
    assert_eq!(
        t.lines().filter(|l| l.starts_with("class=")).count(),
        RULES.len()
    );
    assert!(t.contains("class=normalmap_hq parent=- name=*_nohq.* format=DXT5 channelSwizzleR=1-A channelSwizzleG=G channelSwizzleB=B channelSwizzleA=1-R dynRange=0 mipmapFilter=NormalizeNormalMapAlpha errorMetrics=Distance"));
    assert!(t.contains("class=detail_short parent=detail name=*_dt.*"));
}

// --- swizzle ---------------------------------------------------------------------------------

#[test]
fn swizzle_gives_the_cfg_channels_for_every_class() {
    // Source texel -> stored RGBA per class (forced 0/1 and inversions included). Normal-map
    // classes are renormalised on storing, so they get the flat normal (128, 128, 255), which
    // renormalising leaves alone; the others get (10, 20, 30).
    let cases: &[(&str, [u8; 4], [u8; 4])] = &[
        ("sky", [10, 20, 30, 40], [10, 215, 30, 235]),
        ("detail", [10, 20, 30, 40], [40, 40, 40, 255]),
        ("dt", [10, 20, 30, 40], [40, 40, 40, 255]),
        ("ads", [10, 20, 30, 40], [255, 20, 30, 255]),
        ("adshq", [10, 20, 30, 40], [0, 30, 0, 20]),
        ("dtsmdi", [10, 20, 30, 40], [10, 20, 30, 255]),
        ("as", [10, 20, 30, 40], [255, 20, 255, 255]),
        ("sm", [10, 20, 30, 40], [10, 20, 30, 255]),
        ("smdi", [10, 20, 30, 40], [255, 20, 30, 255]),
        ("co", [10, 20, 30, 40], [10, 20, 30, 40]),
        ("no", [128, 128, 255, 40], [128, 128, 255, 255]),
        ("nohq", [128, 128, 255, 40], [215, 128, 255, 127]),
        ("nshq", [128, 128, 255, 40], [215, 128, 255, 127]),
        ("nofhq", [128, 128, 255, 40], [215, 128, 255, 127]),
        ("novhq", [128, 128, 255, 40], [255, 128, 255, 127]),
        ("nopx", [128, 128, 255, 40], [40, 128, 255, 127]),
        ("nof", [128, 128, 255, 40], [128, 128, 255, 40]),
    ];
    for &(s, src, want) in cases {
        let m = Image {
            w: 128,
            h: 128,
            rgba: src.repeat(128 * 128),
        };
        let out = encode::finish(&m, rule(s));
        assert!(
            out.chunks_exact(4).all(|p| p == want),
            "_{s}: {:?}",
            &out[..4]
        );
    }
}

#[test]
fn swiz_codes_follow_the_stored_channel_order_a_r_g_b() {
    // _nohq stores A' = 1-R, R' = 1-A, G, B: observed SWIZ 05 04 02 03.
    let b = encode::encode(probe_image(0, 16), rule("nohq")).unwrap();
    assert_eq!(header(&b).2, "05040203");
    // Stored RGBA of an opaque _nohq image: R' = 0 (inverted opaque alpha).
    let p = paa::read(&b).unwrap();
    let rgba = crate::decode_mip(
        p.ty,
        &p.mips[2].data,
        p.mips[2].w as usize,
        p.mips[2].h as usize,
    )
    .unwrap();
    assert!(
        rgba.chunks_exact(4).all(|px| px[0] <= 8),
        "stored R is the inverted opaque alpha"
    );
}

#[test]
fn tag_order_is_avgc_maxc_flag_swiz() {
    let b = encode::encode(probe_image(1, 16), rule("sky")).unwrap();
    let names: Vec<String> = paa::read(&b)
        .unwrap()
        .tags
        .into_iter()
        .map(|t| t.name)
        .collect();
    assert_eq!(names, ["AVGC", "MAXC", "FLAG", "SWIZ"]);
}

// --- formats ---------------------------------------------------------------------------------

#[test]
fn pixel_packing_matches_the_observed_quantisation() {
    // Hand-derived from round(c * max / 255); the same rounding matched every texel of
    // ImageToPAA's mip 0 for 4444 and 1555.
    // 4444: A=17 -> 1, R=255 -> 15, G=128 -> 8, B=0 -> 0, packed as bytes [G<<4|B, A<<4|R].
    // 1555: alpha is 1 bit, set above 127. AI88 stores luma (0.299*R + 0.587*G + 0.114*B, so
    // 255 red -> 76) and alpha.
    assert_eq!(argb::pack(&[255, 128, 0, 17], Fmt::Argb4444), [0x80, 0x1F]);
    assert_eq!(argb::pack(&[255, 128, 0, 200], Fmt::Argb1555), [0x00, 0xFE]);
    assert_eq!(argb::pack(&[255, 128, 0, 127], Fmt::Argb1555), [0x00, 0x7E]);
    assert_eq!(argb::pack(&[255, 0, 0, 9], Fmt::Ai88), [76, 9]);
}

#[test]
fn lzss_round_trips_and_checks_the_sum() {
    let mut s = 1u32;
    let noise: Vec<u8> = (0..5000)
        .map(|_| {
            s = s.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            (s >> 24) as u8
        })
        .collect();
    let rep: Vec<u8> = (0..5000).map(|i| (i % 7) as u8 * 40).collect();
    // 4097 identical bytes is a long run of maximum-length (18 byte) matches that reaches one
    // past the 4096-byte window the format uses.
    for raw in [vec![], vec![9], noise, rep.clone(), vec![0xAA; 4097]] {
        let packed = argb::lzss_pack(&raw);
        assert_eq!(argb::lzss_unpack(&packed, raw.len()).unwrap(), raw);
    }
    // 5000 bytes with a 7-byte period should pack to well under a quarter.
    assert!(
        argb::lzss_pack(&rep).len() < 1200,
        "repetitive data must shrink"
    );
    let mut bad = argb::lzss_pack(&rep);
    let n = bad.len();
    bad[n - 1] ^= 1;
    assert!(argb::lzss_unpack(&bad, rep.len()).is_err());
    assert!(argb::lzss_unpack(&bad[..2], rep.len()).is_err());
}

/// Decodes a hex string (two digits per byte) into bytes.
fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

#[test]
fn lzss_reads_a_stream_written_by_imagetopaa() {
    // 4x4 AI88 mip of an opaque gradient from ImageToPAA: literals and back references.
    let data = hex("ff28ff3dff4fff62fffd4e040073ff87ff75ffdf86ff99ffad0400acff0fc0ffd2ffc8ffffff");
    let want = hex("28ff3dff4fff62ff4eff62ff73ff87ff75ff86ff99ffadff99ffacffc0ffd2ff");
    assert_eq!(argb::lzss_unpack(&data, 32).unwrap(), want);
}

#[test]
fn pixel_formats_write_lzss_mips_down_to_one_texel() {
    for (suffix, ty) in [
        ("4444", 0x4444u16),
        ("1555", 0x1555),
        ("88", 0x8080),
        ("gs", 0x8080),
        ("8888", 0x1555),
    ] {
        let img = probe_image(0, 64);
        let b = encode::encode(
            Image {
                w: 64,
                h: 64,
                rgba: img.rgba.clone(),
            },
            rule(suffix),
        )
        .unwrap();
        let p = paa::read(&b).unwrap();
        assert_eq!(p.ty, ty);
        let sizes: Vec<u32> = p.mips.iter().map(|m| m.w).collect();
        assert_eq!(sizes, [64, 32, 16, 8, 4, 2, 1], "_{suffix}");
        assert!(p.mips.iter().all(|m| !m.lzo));
        // Mip 0 decodes to the quantised source.
        let raw = argb::lzss_unpack(&p.mips[0].data, 64 * 64 * 2).unwrap();
        let fmt = match ty {
            0x4444 => Fmt::Argb4444,
            0x1555 => Fmt::Argb1555,
            _ => Fmt::Ai88,
        };
        assert_eq!(raw, argb::pack(&img.rgba, fmt), "_{suffix}");
    }
    // Non-square: the chain ends when the short side is 1.
    let wide = Image {
        w: 16,
        h: 64,
        rgba: vec![90; 16 * 64 * 4],
    };
    let p = paa::read(&encode::encode(wide, rule("4444")).unwrap()).unwrap();
    let dims: Vec<(u32, u32)> = p.mips.iter().map(|m| (m.w, m.h)).collect();
    assert_eq!(dims, [(16, 64), (8, 32), (4, 16), (2, 8), (1, 4)]);
}

#[test]
fn pixel_mips_are_the_box_chain_of_the_8_bit_levels() {
    // Observed exactly (every texel of mips 1 to 3) for ARGB4444: the stored level is
    // quantise(box chain), not box(quantised). This test checks mips 1 and 2.
    let img = probe_image(1, 64);
    let b = encode::encode(
        Image {
            w: 64,
            h: 64,
            rgba: img.rgba.clone(),
        },
        rule("4444"),
    )
    .unwrap();
    let p = paa::read(&b).unwrap();
    let r = rule("4444");
    let mut lvl = mipfilter::next_mip(&img, r.mip_filter, r.linear_mips);
    let raw = argb::lzss_unpack(&p.mips[1].data, 32 * 32 * 2).unwrap();
    assert_eq!(raw, argb::pack(&lvl.rgba, Fmt::Argb4444));
    lvl = mipfilter::next_mip(&lvl, r.mip_filter, r.linear_mips);
    let raw = argb::lzss_unpack(&p.mips[2].data, 16 * 16 * 2).unwrap();
    assert_eq!(raw, argb::pack(&lvl.rgba, Fmt::Argb4444));
}

#[test]
fn uncompressed_default_picks_1555_or_4444_by_alpha() {
    // No format + enableDXT=0 (`_draftlco`, `_noex`, `_nofex`, `_nsex`): opaque and 0/255 alpha
    // 1555, soft alpha 4444 (observed).
    for s in ["draftlco", "noex", "nofex", "nsex"] {
        let tys: Vec<u16> = (0..3)
            .map(|a| header(&encode::encode(probe_image(a, 16), rule(s)).unwrap()).0)
            .collect();
        assert_eq!(tys, [0x1555, 0x4444, 0x1555], "_{s}");
    }
    // An opaque image with a rule that sets no format and no DXT plans 16-bit 1555 pixels.
    assert_eq!(
        encode::plan(rule("draftlco"), &probe_image(0, 16)).layout,
        Layout::Pixels(Fmt::Argb1555)
    );
}

#[test]
fn dxt5_with_autoreduce_is_dxt1_when_opaque_and_flat_images_reduce() {
    assert_eq!(
        header(&encode::encode(probe_image(0, 16), rule("pr")).unwrap()).0,
        paa::TYPE_DXT1
    );
    assert_eq!(
        header(&encode::encode(probe_image(1, 16), rule("pr")).unwrap()).0,
        paa::TYPE_DXT5
    );
    assert_eq!(
        header(&encode::encode(probe_image(0, 16), rule("lca")).unwrap()).0,
        paa::TYPE_DXT5
    );
    let flat = || Image {
        w: 64,
        h: 64,
        rgba: [200, 90, 30, 255].repeat(64 * 64),
    };
    for s in ["mask", "lco", "mco", "pr"] {
        let p = paa::read(&encode::encode(flat(), rule(s)).unwrap()).unwrap();
        assert_eq!(p.mips.len(), 1, "_{s}");
        assert_eq!((p.mips[0].w, p.mips[0].h), (4, 4));
    }
    // Not for `_co` (no autoreduce), and not for an image with any variation.
    assert_eq!(
        paa::read(&encode::encode(flat(), rule("co")).unwrap())
            .unwrap()
            .mips
            .len(),
        5
    );
    assert_eq!(
        paa::read(&encode::encode(probe_image(0, 64), rule("mask")).unwrap())
            .unwrap()
            .mips
            .len(),
        5
    );
}

#[test]
fn avgc_is_linear_for_dyn_range_1_and_plain_for_0() {
    // Left half black, right half white: plain mean 128 (127.5 rounds up), linear-light mean 188.
    let mut rgba = Vec::new();
    for _ in 0..16 {
        for x in 0..16 {
            let v = if x < 8 { 0 } else { 255 };
            rgba.extend_from_slice(&[v, v, v, 255]);
        }
    }
    let avg = |s: &str| {
        let b = encode::encode(
            Image {
                w: 16,
                h: 16,
                rgba: rgba.clone(),
            },
            rule(s),
        )
        .unwrap();
        paa::read(&b).unwrap().tags[0].data.clone()
    };
    assert_eq!(avg("co"), [188, 188, 188, 255]);
    assert_eq!(avg("cdt"), [128, 128, 128, 255]);
    assert_eq!(
        avg("gs"),
        [188, 188, 188, 255],
        "dynRange unset behaves as 1 (observed)"
    );
}

// --- mip filters -----------------------------------------------------------------------------

/// A texel holding the unit vector with the given x and y (z up), alpha `a`.
fn normal_texel(x: f64, y: f64, a: u8) -> [u8; 4] {
    let z = (1.0 - x * x - y * y).sqrt();
    let e = |v: f64| ((v * 0.5 + 0.5) * 255.0).round() as u8;
    [e(x), e(y), e(z), a]
}

#[test]
fn normalize_normal_map_chain_is_a_byte_box_and_output_is_renormalised() {
    let l = normal_texel(-0.5, 0.0, 255);
    let r = normal_texel(0.5, 0.0, 255);
    let img = Image {
        w: 2,
        h: 2,
        rgba: [l, r, l, r].concat(),
    };
    // The mip chain level is the plain byte box mean (128,128,238: z falls short of unit
    // length). Its R is 127 because the chain truncates R and G but rounds B and A.
    let m = mipfilter::next_mip(&img, Some(Filter::NormalizeNormalMap), false);
    assert_eq!(m.rgba, [127, 128, 238, 255]); // B and A round; R and G truncate
    assert!(m.rgba[2] < 250);
    // Storing it renormalises: opposite tilts cancel to the flat normal.
    let mut out = m.rgba.clone();
    mipfilter::output(&mut out, Some(Filter::NormalizeNormalMap), 4, 4);
    assert_eq!(out, [127, 128, 255, 255]);
    // Level 0 is renormalised too: a short vector is stretched to unit length.
    let mut short = [160u8, 128, 140, 255];
    mipfilter::output(&mut short, Some(Filter::NormalizeNormalMapAlpha), 4, 4);
    assert!(short[0] > 160 && short[2] > 140);
}

#[test]
fn normalize_normal_map_alpha_weights_the_chain_by_alpha() {
    let l = normal_texel(-0.5, 0.0, 0);
    let r = normal_texel(0.5, 0.0, 255);
    let img = Image {
        w: 2,
        h: 2,
        rgba: [l, r, l, r].concat(),
    };
    let a = mipfilter::next_mip(&img, Some(Filter::NormalizeNormalMapAlpha), false);
    // Only the right-tilted texels count: the result is that texel (alpha is the box mean).
    assert_eq!(&a.rgba[..3], &r[..3]);
    assert_eq!(a.rgba[3], ((255 * 2 + 2) / 4) as u8);
    // The plain normal-map filter ignores alpha: the same image with every alpha made opaque
    // gives the same RGB.
    let p = mipfilter::next_mip(&img, Some(Filter::NormalizeNormalMap), false);
    let mut opaque = Image {
        w: 2,
        h: 2,
        rgba: img.rgba.clone(),
    };
    opaque.rgba.chunks_exact_mut(4).for_each(|t| t[3] = 255);
    assert_eq!(
        &p.rgba[..3],
        &mipfilter::next_mip(&opaque, Some(Filter::NormalizeNormalMap), false).rgba[..3]
    );
    // All-transparent group: falls back to the unweighted mean instead of dividing by zero.
    let z = Image {
        w: 2,
        h: 2,
        rgba: [normal_texel(0.5, 0.0, 0); 4].concat(),
    };
    assert_eq!(
        &mipfilter::next_mip(&z, Some(Filter::NormalizeNormalMapAlpha), false).rgba[..3],
        &r[..3]
    );
}

#[test]
fn linear_mips_average_in_linear_light() {
    // Black and white: the byte box gives 127 for R (R and G truncate, 127.5) and 128 for B, linear light gives 188 (sRGB of 0.5).
    let img = Image {
        w: 2,
        h: 2,
        rgba: [
            [0, 0, 0, 255],
            [255, 255, 255, 255],
            [0, 0, 0, 255],
            [255, 255, 255, 255],
        ]
        .concat(),
    };
    assert_eq!(
        mipfilter::next_mip(&img, None, false).rgba[..3],
        [127, 127, 128]
    );
    assert_eq!(mipfilter::next_mip(&img, None, true).rgba[0], 188);
    // Alpha weighted: a transparent white texel does not brighten the result.
    let a = Image {
        w: 2,
        h: 2,
        rgba: [
            [0, 0, 0, 255],
            [255, 255, 255, 0],
            [0, 0, 0, 255],
            [255, 255, 255, 0],
        ]
        .concat(),
    };
    assert_eq!(
        mipfilter::next_mip(&a, None, true).rgba[..4],
        [0, 0, 0, 128]
    );
    // The classes that use it.
    let on: Vec<&str> = RULES
        .iter()
        .filter(|r| r.linear_mips)
        .map(|r| r.suffix())
        .collect();
    assert_eq!(on, ["8888", "co", "ca", "cat", "gs", "can"]);
}

#[test]
fn fade_amounts_and_directions() {
    assert_eq!(mipfilter::fade_amount(4096), 0.0);
    assert_eq!(mipfilter::fade_amount(128), 0.0);
    let table: Vec<f64> = [64, 32, 16, 8, 4, 2, 1]
        .map(mipfilter::fade_amount)
        .to_vec();
    assert_eq!(table, [0.2, 0.25, 0.33, 0.5, 0.75, 0.875, 1.0]);
    // FadeOut pulls every channel toward 128.
    let mut px = [228u8, 28, 128, 200].repeat(4);
    mipfilter::output(&mut px, Some(Filter::FadeOut), 4, 4);
    assert_eq!(&px[..4], &[153, 103, 128, 146]);
    // Big mips are untouched.
    let mut big = [228u8, 28, 128, 200].repeat(4);
    mipfilter::output(&mut big, Some(Filter::FadeOut), 256, 256);
    assert_eq!(&big[..4], &[228, 28, 128, 200]);
    // The normal fade moves the vector toward (0, 0, 1) and keeps unit length; without the fade
    // it only renormalises.
    let t = normal_texel(0.6, 0.0, 255);
    let mut n = t.repeat(4);
    mipfilter::output(&mut n, Some(Filter::NormalizeNormalMapFade), 4, 4);
    assert!(n[0] < t[0] && n[0] > 128 && n[2] > t[2]);
    let mut plain = t.repeat(4);
    mipfilter::output(&mut plain, Some(Filter::NormalizeNormalMap), 4, 4);
    assert!(plain[0].abs_diff(t[0]) <= 1 && plain[2].abs_diff(t[2]) <= 1);
    // At 1x1 the fade is complete: the flat normal.
    let mut one = t.to_vec();
    mipfilter::output(&mut one, Some(Filter::NormalizeNormalMapFade), 1, 1);
    assert_eq!(&one[..3], &[128, 128, 255]);
}

#[test]
fn fade_applies_to_small_mips_of_detail_textures_only() {
    let img = probe_image(0, 64);
    let mip = |s: &str, i: usize| {
        let p = paa::read(
            &encode::encode(
                Image {
                    w: 64,
                    h: 64,
                    rgba: img.rgba.clone(),
                },
                rule(s),
            )
            .unwrap(),
        )
        .unwrap();
        crate::decode_mip(
            p.ty,
            &p.mips[i].data,
            p.mips[i].w as usize,
            p.mips[i].h as usize,
        )
        .unwrap()
    };
    // Last mip (4x4): `_cdt` (FadeOut) is closer to mid grey than `_mco` (same format, no filter).
    let dist = |v: &[u8]| {
        v.chunks_exact(4)
            .map(|p| (p[0] as i32 - 128).abs() + (p[1] as i32 - 128).abs())
            .sum::<i32>()
    };
    assert!(dist(&mip("cdt", 4)) * 2 < dist(&mip("mco", 4)));
    // Mip 0 of a 64x64 `_cdt` is faded too (the fade follows the mip size, observed).
    assert!(dist(&mip("cdt", 0)) < dist(&mip("mco", 0)));
}

#[test]
fn rule_flags_are_consistent() {
    for r in RULES {
        assert!(
            r.pattern.starts_with("*_") && r.pattern.ends_with(".*"),
            "{}",
            r.class
        );
        // The noise filters are encoded (`noise`): no rule is unreplicable, and each is verified.
        let noise = matches!(
            r.mip_filter,
            Some(Filter::AlphaNoise | Filter::AddAlphaNoise | Filter::NormalizeNormalMapNoise)
        );
        assert!(
            r.unreplicable().is_none() && (!noise || r.verified),
            "{}",
            r.class
        );
    }
}

#[test]
fn chain_box_truncates_r_and_g_and_rounds_b_and_a() {
    // Sum 2 of 4 in every channel: 0.5 rounds up for B and A, down (truncates) for R and G.
    let img = Image {
        w: 2,
        h: 2,
        rgba: [[1, 1, 1, 1], [1, 1, 1, 1], [0, 0, 0, 0], [0, 0, 0, 0]].concat(),
    };
    assert_eq!(mipfilter::next_mip(&img, None, false).rgba, [0, 0, 1, 1]);
}

// --- no hand-over for alpha -------------------------------------------------------

// Loki once refused DXT1 1-bit alpha and soft alpha (AVGC) per file. It now reproduces the soft
// alpha AVGC exactly and encodes the 1-bit mask with its own deterministic threshold, and a hand-over is allowed
// a hand-over only when Loki cannot produce a valid file. So every class of the oracle table, at every alpha
// kind (opaque, soft ramp, 0/255), encodes here, and ImageToPAA's own FLAG and type words come out equal
// (`structure_equals_imagetopaa_for_every_suffix`); no class is refused.
#[test]
fn every_class_encodes_every_alpha_kind_without_a_hand_over() {
    let mut bad = Vec::new();
    for &(suffix, _, _) in STRUCTURE {
        let r = rule(suffix);
        if r.unreplicable().is_some() {
            continue;
        }
        for alpha in 0..3 {
            if let Err(e) = encode::encode(probe_image(alpha, 16), r) {
                bad.push(format!("_{suffix} alpha{alpha}: {e}"));
            }
        }
    }
    for alpha in 0..3 {
        if let Err(e) = encode::encode(probe_image(alpha, 16), &rules::PLAIN) {
            bad.push(format!("(none) alpha{alpha}: {e}"));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

// `Rule::linear_mips` and `dynRange` say the same thing on every verified row.
#[test]
fn linear_mips_is_dyn_range_not_zero_on_every_verified_row() {
    for r in RULES.iter().filter(|r| r.verified).chain([&rules::PLAIN]) {
        assert_eq!(r.linear_mips, r.dyn_range != Some(0), "{}", r.class);
    }
}
