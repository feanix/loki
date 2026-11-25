//! Tests for images with a side under 4 px (0.3.1, finding F4 of the #40 deep scan).
//!
//! What ImageToPAA does there (observed on 40 classes, sizes 1x1, 2x2, 1x256, 256x1, 4x1, alpha
//! opaque, 0/255 and soft):
//! * A rule that forces a DXT format (`_co`, `_ca`, `_nohq`, `_dxt1`, ...) fails and leaves a
//!   truncated file. Loki refuses these with a message naming the 4 px minimum.
//! * Every other rule writes an ARGB file: ARGB1555 for opaque or 0/255 alpha, ARGB4444 for soft
//!   alpha (the forced ARGB/AI88 classes keep their own format), FLAG as for larger images, and a
//!   mip chain that halves until a side is 1 (2x2 gives 2x2 and 1x1, 1x256 and 256x1 one mip).
//!
//! The expectations come from ImageToPAA output; the comparison table is in
//! research/rust-deep/logs/sizes-045/README.md. Inputs here are generated, no fixtures.

use crate::encode;
use crate::error::Error;
use crate::img::Image;
use crate::paa;
use crate::tests::rule;
use std::path::PathBuf;

/// A w x h image of a grey ramp with the given alpha pattern: "opaque", "bin" (0/255) or "soft".
fn ramp(w: usize, h: usize, kind: &str) -> Image {
    let n = w * h;
    let mut rgba = Vec::with_capacity(n * 4);
    for i in 0..n {
        let v = (30 + i * 190 / n.max(2).saturating_sub(1).max(1)) as u8;
        let a = match kind {
            "opaque" => 255,
            "bin" => {
                if i % 2 == 1 {
                    255
                } else {
                    0
                }
            }
            _ => (60 + (i * 7) % 150) as u8,
        };
        rgba.extend_from_slice(&[v, v, v, a]);
    }
    Image { w, h, rgba }
}

/// The mip sizes of an ARGB chain: halve until either side is 1.
fn pixel_chain(mut w: u32, mut h: u32) -> Vec<(u32, u32)> {
    let mut v = vec![(w, h)];
    while w > 1 && h > 1 {
        w /= 2;
        h /= 2;
        v.push((w, h));
    }
    v
}

fn flag(p: &paa::Paa) -> Option<u32> {
    p.tags
        .iter()
        .find(|t| t.name == "FLAG")
        .map(|t| u32::from_le_bytes(t.data[..4].try_into().unwrap()))
}

const SIZES: [(usize, usize); 6] = [(1, 1), (2, 2), (1, 256), (256, 1), (4, 1), (2, 8)];

#[test]
fn small_sides_get_the_oracle_type_flag_and_chain_for_every_non_dxt_family() {
    // (suffix, type word for opaque, for 0/255 alpha, for soft alpha, FLAG for 0/255, FLAG for soft)
    // Observed with ImageToPAA. "" and the normal-map classes are DXT by default and fall back to ARGB.
    let rows: [(&str, u16, u16, u16, u32, u32); 12] = [
        ("", 0x1555, 0x1555, 0x4444, 2, 1),
        ("normalmap", 0x1555, 0x1555, 0x4444, 2, 1),
        ("nof", 0x1555, 0x1555, 0x4444, 2, 1),
        ("ns", 0x1555, 0x1555, 0x4444, 2, 1),
        ("draftlco", 0x1555, 0x1555, 0x4444, 2, 1),
        ("raw", 0x1555, 0x1555, 0x1555, 2, 2),
        ("8888", 0x1555, 0x1555, 0x1555, 2, 2),
        ("1555", 0x1555, 0x1555, 0x1555, 2, 2),
        ("4444", 0x4444, 0x4444, 0x4444, 2, 1),
        ("88", 0x8080, 0x8080, 0x8080, 2, 1),
        ("gs", 0x8080, 0x8080, 0x8080, 2, 1),
        ("noex", 0x1555, 0x1555, 0x4444, 2, 1),
    ];
    for (suffix, t_opaque, t_bin, t_soft, f_bin, f_soft) in rows {
        for (w, h) in SIZES {
            for kind in ["opaque", "bin", "soft"] {
                let bytes = encode::encode(ramp(w, h, kind), rule(suffix))
                    .unwrap_or_else(|e| panic!("_{suffix} {w}x{h} {kind}: {e}"));
                let p = paa::read(&bytes).unwrap();
                let (ty, fl) = match kind {
                    "opaque" => (t_opaque, None),
                    "bin" => (t_bin, Some(f_bin)),
                    _ => (t_soft, Some(f_soft)),
                };
                let ctx = format!("_{suffix} {w}x{h} {kind}");
                assert_eq!(p.ty, ty, "{ctx}: type");
                assert_eq!(flag(&p), fl, "{ctx}: FLAG");
                let got: Vec<(u32, u32)> = p.mips.iter().map(|m| (m.w, m.h)).collect();
                assert_eq!(got, pixel_chain(w as u32, h as u32), "{ctx}: mips");
                assert!(
                    p.mips.iter().all(|m| !m.lzo),
                    "{ctx}: pixel formats are never LZO"
                );
            }
        }
    }
}

#[test]
fn small_side_pixels_read_back_within_the_format_tolerance() {
    // Quantisation: 5 bits (1555) moves a level by up to 4, 4 bits (4444) by up to 8.
    for suffix in ["raw", "4444", "gs", ""] {
        for (w, h) in SIZES {
            let img = ramp(w, h, "opaque");
            let src = img.rgba.clone();
            let p = paa::read(&encode::encode(img, rule(suffix)).unwrap()).unwrap();
            let m = &p.mips[0];
            let back = crate::decode_mip(p.ty, &m.data, m.w as usize, m.h as usize).unwrap();
            assert_eq!(back.len(), src.len(), "_{suffix} {w}x{h}");
            for (a, b) in src.iter().zip(&back) {
                assert!(
                    (*a as i32 - *b as i32).abs() <= 8,
                    "_{suffix} {w}x{h}: {a} vs {b}"
                );
            }
        }
    }
}

#[test]
fn a_2x2_chain_ends_in_the_box_average() {
    // Opaque 2x2 of four greys in `_raw`: mip 1 is the byte box mean of the four (R, G truncate).
    let img = Image {
        w: 2,
        h: 2,
        rgba: [40u8, 80, 120, 200]
            .iter()
            .flat_map(|&v| [v, v, v, 255])
            .collect(),
    };
    let p = paa::read(&encode::encode(img, rule("raw")).unwrap()).unwrap();
    let m = &p.mips[1];
    let back = crate::decode_mip(p.ty, &m.data, 1, 1).unwrap();
    // mean 110; the 5 bit grey nearest to it is within 4
    assert!(
        (back[0] as i32 - 110).abs() <= 4 && (back[2] as i32 - 110).abs() <= 4,
        "{back:?}"
    );
}

#[test]
fn forced_dxt_rules_refuse_a_small_side_with_the_4_px_message() {
    for suffix in ["co", "ca", "dxt1", "dxt5", "nohq", "lco", "mask", "dt"] {
        for (w, h) in [(1, 1), (2, 2), (1, 256), (256, 1), (4, 1), (1, 4), (2, 8)] {
            let e = encode::encode(ramp(w, h, "opaque"), rule(suffix))
                .err()
                .unwrap_or_else(|| panic!("_{suffix} {w}x{h} was accepted"));
            assert!(matches!(e, Error::Size(_)), "_{suffix} {w}x{h}: {e}");
            let t = e.to_string();
            assert!(
                t.contains(&format!("{w}x{h}")) && t.contains("DXT") && t.contains("at least 4 px"),
                "{t}"
            );
            assert!(!t.contains("power"), "{t}");
        }
    }
}

#[test]
fn the_power_of_two_text_is_only_for_sizes_that_are_not_a_power_of_two() {
    for (w, h) in [(3, 3), (100, 100), (0, 4), (6, 1)] {
        let e = encode::encode(
            Image {
                w,
                h,
                rgba: vec![0; w * h * 4],
            },
            rule("raw"),
        )
        .err()
        .unwrap();
        assert!(e.to_string().contains("power-of-two"), "{w}x{h}: {e}");
    }
}

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("loki-paa-small-{}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    d.join(name)
}

/// A grey PNG of w x h written through the crate (RGBA8).
fn write_png(path: &std::path::Path, w: usize, h: usize) {
    crate::img::save_png(path, &ramp(w, h, "opaque")).unwrap();
}

#[test]
fn encode_file_writes_a_small_non_dxt_file_and_leaves_nothing_for_a_dxt_refusal() {
    // Both outcomes come from `encode_file`: a DXT rule on a tiny image is a size refusal, never a hand-over.
    for (name, w, h) in [("a_raw.png", 1, 1), ("b_raw.png", 256, 1), ("c.png", 2, 2)] {
        let (png, out) = (scratch(name), scratch(&format!("{name}.paa")));
        write_png(&png, w, h);
        let _ = std::fs::remove_file(&out);
        crate::encode_file(&png, &out, None).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert!(paa::read(&std::fs::read(&out).unwrap()).is_ok(), "{name}");
    }
    for (name, w, h) in [
        ("d_co.png", 1, 1),
        ("e_ca.png", 256, 1),
        ("f_nohq.png", 2, 2),
    ] {
        let (png, out) = (scratch(name), scratch(&format!("{name}.paa")));
        write_png(&png, w, h);
        let _ = std::fs::remove_file(&out);
        let e = crate::encode_file(&png, &out, None)
            .err()
            .unwrap_or_else(|| panic!("{name} was accepted"));
        assert!(matches!(e, Error::Size(_)), "{name}: {e}");
        assert!(!out.exists(), "{name}: a refused encode leaves no file");
    }
    // A DXT1 alpha image (1-bit alpha) on a tiny side is a size refusal too, not a hand-over.
    let (png, out) = (scratch("g_co.png"), scratch("g_co.png.paa"));
    crate::img::save_png(&png, &ramp(2, 2, "bin")).unwrap();
    assert!(matches!(
        crate::encode_file(&png, &out, None),
        Err(Error::Size(_))
    ));
}

#[test]
fn encode_file_encodes_a_soft_alpha_unsuffixed_small_image() {
    // Soft alpha on a tiny unsuffixed image goes to ARGB4444 and its AVGC is the soft alpha one (#31); no hand-over.
    let (png, out) = (scratch("h.png"), scratch("h.png.paa"));
    crate::img::save_png(&png, &ramp(2, 2, "soft")).unwrap();
    crate::encode_file(&png, &out, None).unwrap();
    assert!(paa::read(&std::fs::read(&out).unwrap()).is_ok());
}
