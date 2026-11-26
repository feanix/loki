//! Hostile-input and round-trip tests.
//!
//! Inputs are generated here, no binary fixtures. The hostile cases exist because
//! the encoder will run inside Loki's process, where an abort or panic kills the app.

use crate::encode::{self, Layout};
use crate::error::Error;
use crate::img::{self, Image};
use crate::paa;
use crate::rules::{self, Rule};

/// The rule of a suffix ("" = unsuffixed).
pub(crate) fn rule(suffix: &str) -> &'static Rule {
    if suffix.is_empty() {
        &rules::PLAIN
    } else {
        rules::find(suffix).unwrap()
    }
}

/// CRC-32 (PNG chunk checksum), bitwise; only used to build tiny test files.
pub(crate) fn crc32(data: &[u8]) -> u32 {
    let mut c = !0u32;
    for &b in data {
        c ^= b as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 {
                (c >> 1) ^ 0xEDB8_8320
            } else {
                c >> 1
            };
        }
    }
    !c
}

/// Append one PNG chunk: big-endian length, name, data, then a CRC-32 over name and data (the
/// length is not covered).
pub(crate) fn chunk(out: &mut Vec<u8>, name: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    let mut body = name.to_vec();
    body.extend_from_slice(data);
    out.extend_from_slice(&body);
    out.extend_from_slice(&crc32(&body).to_be_bytes());
}

/// Header-only PNG (signature, IHDR, junk IDAT, IEND; 68 bytes) declaring `w`x`h` 8-bit RGBA.
pub(crate) fn header_only_png(w: u32, h: u32) -> Vec<u8> {
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&w.to_be_bytes());
    ihdr.extend_from_slice(&h.to_be_bytes());
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ihdr);
    // read_info() stops at the first IDAT, so a short junk body is enough.
    chunk(
        &mut out,
        b"IDAT",
        &[0x78, 0x9c, 0x63, 0, 0, 0, 1, 0, 1, 0, 0],
    );
    chunk(&mut out, b"IEND", &[]);
    out
}

/// Write `bytes` to a uniquely named temp file, run the PNG loader on it, and return only
/// success or the error. The file is removed afterwards.
pub(crate) fn load_from_bytes(name: &str, bytes: &[u8]) -> Result<(), Error> {
    let path =
        std::env::temp_dir().join(format!("paa-proto-test-{}-{name}.png", std::process::id()));
    std::fs::write(&path, bytes).unwrap();
    let r = img::load_png(&path).map(|_| ());
    let _ = std::fs::remove_file(&path);
    r
}

// The loader must reject by header size alone, before decoding any pixels. 200000x200000 would
// overflow or exhaust memory; 20000x20000 is a plausible size that is still over the 16384
// per-side limit.
#[test]
fn png_header_huge_is_error() {
    let bytes = header_only_png(200_000, 200_000);
    assert_eq!(bytes.len(), 68);
    assert!(load_from_bytes("huge", &bytes)
        .unwrap_err()
        .to_string()
        .contains("200000x200000"));
}

#[test]
fn png_header_over_cap_is_error() {
    let e = load_from_bytes("20k", &header_only_png(20_000, 20_000)).unwrap_err();
    assert!(e.to_string().contains("20000x20000"), "{e}");
}

#[test]
fn png_header_zero_is_error() {
    assert!(load_from_bytes("zero", &header_only_png(0, 16)).is_err());
    assert!(load_from_bytes("zero2", &header_only_png(16, 0)).is_err());
}

#[test]
fn offs_length_not_multiple_of_4_is_error() {
    for len in [1u32, 2, 3, 5] {
        // A PAA with an OFFS tag whose data length is not a whole number of u32 entries. Tag
        // names are stored reversed on disk ("GGAT" is TAGG, "SFFO" is OFFS). The 8 zero bytes
        // after the tag stand in for the palette count and the end marker.
        let mut b = paa::TYPE_DXT1.to_le_bytes().to_vec();
        b.extend_from_slice(b"GGAT");
        b.extend_from_slice(b"SFFO");
        b.extend_from_slice(&len.to_le_bytes());
        b.extend(std::iter::repeat_n(0u8, len as usize));
        b.extend_from_slice(&[0u8; 8]);
        assert!(paa::read(&b).is_err(), "len {len}");
    }
}

/// Test image of w x h pixels with a repeating colour pattern (so mips differ from a flat
/// colour) and a constant alpha `a`.
pub(crate) fn solid(w: usize, h: usize, a: u8) -> Image {
    let rgba = (0..w * h)
        .flat_map(|i| [(i % 251) as u8, (i % 13) as u8 * 9, 40, a])
        .collect();
    Image { w, h, rgba }
}

#[test]
fn round_trip_co_and_ca() {
    // 64x64 halves to 4x4: 64, 32, 16, 8, 4 = 5 mips.
    for (suffix, ty) in [("co", paa::TYPE_DXT1), ("ca", paa::TYPE_DXT5)] {
        let bytes = encode::encode(solid(64, 64, 255), rule(suffix)).unwrap();
        let p = paa::read(&bytes).unwrap();
        assert_eq!(p.ty, ty);
        assert_eq!(p.mips.len(), 5);
        assert_eq!((p.mips[0].w, p.mips[0].h), (64, 64));
        assert_eq!((p.mips[4].w, p.mips[4].h), (4, 4));
    }
}

// --- classify, supported, plan --------------------------------------------------------------

#[test]
fn classify_table() {
    let ok = |s: &str| encode::classify(s);
    assert_eq!(ok("rock_co").class, "ColorMap");
    assert_eq!(ok("ROCK_CA").class, "ColorAlphaMap");
    assert_eq!(ok("rock_nohq").class, "normalmap_hq");
    assert_eq!(ok("rock_dt").class, "detail_short");
    assert_eq!(ok("rock_smdi").class, "specular_diffuseinverse_map");
    assert_eq!(ok("rock").class, "(none)");
    assert_eq!(ok("rock_plain").class, "(none)");
    assert_eq!(ok("rock_").class, "(none)");
}

/// ImageToPAA gave `_foo _xyz _co2 _nrm _caa _cox _nohq2` the unsuffixed output. One place
/// (`rules::for_suffix`) says so; classify, supported and encode follow it.
#[test]
fn unknown_suffix_is_the_default_rule() {
    for s in ["foo", "xyz", "co2", "nrm", "caa", "cox", "nohq2", "", "CO2"] {
        assert_eq!(rules::for_suffix(s).class, "(none)", "{s}");
        assert_eq!(
            encode::classify(&format!("rock_{s}")).class,
            "(none)",
            "{s}"
        );
    }
    assert_eq!(rules::for_suffix("co").class, "ColorMap");
}

#[test]
fn supported_follows_the_verified_rules() {
    for (name, want) in [
        ("rock_co.png", true),
        ("rock_ca.png", true),
        ("ROCK_CO.PNG", true),
        ("rock.png", true),
        ("rock", true),
        ("rock_co", true),
        ("dir/sub\\x_ca.png", true),
        ("rock_nohq.png", true),
        ("rock_dt.png", true),
        ("rock_smdi.png", true),
        ("rock_sky.png", true),
        ("rock_cat.png", true),
        ("rock_can.png", true),
        ("rock_non.png", true),
        // Unknown suffixes get the default rule, which ImageToPAA applies too.
        ("rock_foo.png", true),
        ("rock_.png", true),
        ("rock_co2.png", true),
        ("rock_nohq2.png", true),
        ("rock_co.jpg", false),
    ] {
        assert_eq!(crate::supported(name), want, "{name}");
    }
}

/// Image four rows high (a side under 4 px is no DXT image), one pixel per entry of `a` in each row,
/// constant colour with that alpha.
fn with_alpha(a: &[u8]) -> Image {
    let rgba = (0..4)
        .flat_map(|_| a.iter().flat_map(|&a| [10, 20, 30, a]))
        .collect();
    Image {
        w: a.len(),
        h: 4,
        rgba,
    }
}

// Alpha 240 is the cutoff: a pixel at 240 or more is treated as opaque, anything lower (239)
// makes the encoder record a FLAG tag. The flag value is 2 for punch-through (DXT1, ARGB1555, 0/255
// alpha) and 1 for soft alpha (DXT5), or none. `faint` and `flagged` sit either side of the cutoff.
#[test]
fn plan_thresholds() {
    use crate::dxt::Format;
    let opaque = with_alpha(&[255; 16]);
    let faint = with_alpha(&[255, 240, 255, 255]);
    let flagged = with_alpha(&[255, 239, 255, 255]);
    let binary = with_alpha(&[0, 255, 255, 0]);
    let soft = with_alpha(&[0, 100, 255, 255]);
    let p = |s: &str, i: &Image| {
        let pl = encode::plan(rule(s), i);
        let Layout::Block(f) = pl.layout else {
            panic!("pixel layout")
        };
        (f, pl.flag)
    };
    assert_eq!(p("co", &opaque), (Format::Dxt1 { punch: false }, None));
    assert_eq!(p("co", &faint), (Format::Dxt1 { punch: false }, None));
    assert_eq!(p("co", &flagged), (Format::Dxt1 { punch: true }, Some(2)));
    assert_eq!(p("ca", &opaque), (Format::Dxt5, None));
    assert_eq!(p("ca", &faint), (Format::Dxt5, None));
    assert_eq!(p("ca", &flagged), (Format::Dxt5, Some(1)));
    assert_eq!(p("nohq", &soft), (Format::Dxt5, Some(1)));
    assert_eq!(p("", &opaque), (Format::Dxt1 { punch: false }, None));
    assert_eq!(p("", &binary), (Format::Dxt1 { punch: true }, Some(2)));
    assert_eq!(p("", &soft), (Format::Dxt5, Some(1)));
    assert_eq!(p("", &faint), (Format::Dxt5, None));
}

// --- limit_size and encode preconditions ------------------------------------------------------

/// Image of the given size with every byte 128 (mid-grey at half alpha); only the size matters.
pub(crate) fn blank(w: usize, h: usize) -> Image {
    Image {
        w,
        h,
        rgba: vec![128; w * h * 4],
    }
}

#[test]
fn limit_size_halves_and_refuses() {
    let i = encode::limit_size(blank(64, 32), 16, rule("co")).unwrap();
    assert_eq!((i.w, i.h), (16, 8));
    let i = encode::limit_size(blank(64, 32), 64, rule("co")).unwrap();
    assert_eq!((i.w, i.h), (64, 32));
    assert!(matches!(
        encode::limit_size(blank(16, 8), 4, rule("co")),
        Err(Error::Size(_))
    ));
    assert!(matches!(
        encode::limit_size(blank(13, 13), 8, rule("co")),
        Err(Error::Size(_))
    ));
    assert!(matches!(
        encode::limit_size(blank(64, 64), 0, rule("co")),
        Err(Error::Size(_))
    ));
}

#[test]
fn encode_rejects_bad_sizes_without_panic() {
    for (w, h) in [(0, 0), (3, 4), (4, 3), (6, 8), (8192, 4), (4, 8192)] {
        let img = Image {
            w,
            h,
            rgba: vec![0; (w * h * 4).min(1 << 16)],
        };
        assert!(
            matches!(encode::encode(img, rule("co")), Err(Error::Size(_))),
            "{w}x{h}"
        );
    }
    // pixel buffer shorter than w*h*4
    let short = Image {
        w: 8,
        h: 8,
        rgba: vec![0; 100],
    };
    assert!(matches!(
        encode::encode(short, rule("ca")),
        Err(Error::Size(_))
    ));
}

#[test]
fn encode_minimum_size_works() {
    let bytes = encode::encode(blank(4, 4), rule("ca")).unwrap();
    assert_eq!(paa::read(&bytes).unwrap().mips.len(), 1);
}

// --- PAA container -----------------------------------------------------------------------------

fn small_paa() -> Vec<u8> {
    encode::encode(solid(16, 16, 255), rule("co")).unwrap()
}

#[test]
fn write_read_round_trip_keeps_tags_and_mips() {
    let p = paa::Paa {
        ty: paa::TYPE_DXT5,
        tags: vec![paa::Tag {
            name: "AVGC".into(),
            data: vec![1, 2, 3, 4],
        }],
        mips: vec![
            paa::Mip {
                w: 8,
                h: 4,
                lzo: false,
                data: vec![7; 32],
            },
            paa::Mip {
                w: 4,
                h: 4,
                lzo: true,
                data: vec![9; 5],
            },
        ],
    };
    let q = paa::read(&paa::write(&p).unwrap()).unwrap();
    assert_eq!(q.ty, paa::TYPE_DXT5);
    assert_eq!(
        (q.tags[0].name.as_str(), q.tags[0].data.as_slice()),
        ("AVGC", &[1u8, 2, 3, 4][..])
    );
    assert_eq!(q.mips.len(), 2);
    assert_eq!(
        (
            q.mips[0].w,
            q.mips[0].h,
            q.mips[0].lzo,
            q.mips[0].data.len()
        ),
        (8, 4, false, 32)
    );
    assert_eq!(
        (
            q.mips[1].w,
            q.mips[1].h,
            q.mips[1].lzo,
            q.mips[1].data.len()
        ),
        (4, 4, true, 5)
    );
}

#[test]
fn write_refuses_fields_that_do_not_fit() {
    let mip = |w: u32, h: u32, n: usize| paa::Mip {
        w,
        h,
        lzo: false,
        data: vec![0; n],
    };
    let one = |m| paa::Paa {
        ty: paa::TYPE_DXT1,
        tags: vec![],
        mips: vec![m],
    };
    // exactly the u24 limit is fine, one over is the typed error
    assert!(paa::write(&one(mip(4, 4, 0xFF_FFFF))).is_ok());
    match paa::write(&one(mip(4096, 4096, 0x100_0000))) {
        Err(Error::MipTooLarge {
            w: 4096,
            h: 4096,
            len: 0x100_0000,
        }) => {}
        other => panic!("{other:?}"),
    }
    assert!(matches!(
        paa::write(&one(mip(0x8000, 4, 1))),
        Err(Error::Paa(_))
    ));
    assert!(matches!(
        paa::write(&one(mip(4, 0x1_0000, 1))),
        Err(Error::Paa(_))
    ));
    let many = paa::Paa {
        ty: paa::TYPE_DXT1,
        tags: vec![],
        mips: (0..17).map(|_| mip(4, 4, 1)).collect(),
    };
    assert!(matches!(paa::write(&many), Err(Error::Paa(_))));
    let bad_tag = paa::Paa {
        ty: paa::TYPE_DXT1,
        tags: vec![paa::Tag {
            name: "TOOLONG".into(),
            data: vec![],
        }],
        mips: vec![],
    };
    assert!(matches!(paa::write(&bad_tag), Err(Error::Paa(_))));
}

#[test]
fn read_corrupt_tables_is_error_never_panic() {
    let good = small_paa();
    assert!(paa::read(&good).is_ok());
    // Every truncation must be an error. The file ends with 6 zero bytes, but the reader stops
    // after the first 4 (a zero width and zero height), so cutting only the last 1 or 2 still
    // parses. The loop therefore stops 2 short of the full length.
    for n in 0..good.len() - 2 {
        assert!(paa::read(&good[..n]).is_err(), "truncated to {n}");
    }
    // every single-byte corruption must not panic (result may be Ok or Err)
    for i in 0..good.len() {
        let mut b = good.clone();
        b[i] ^= 0xFF;
        let _ = paa::read(&b);
    }
    // palette count set. Skip the OFFS tag name (4) and length (4), then its 64 bytes of offsets
    // (16 entries), to land on the palette count.
    let mut b = good.clone();
    let off = b.windows(4).position(|w| w == b"SFFO").unwrap() + 4 + 4 + 64;
    b[off] = 1;
    assert!(matches!(paa::read(&b), Err(Error::Paa(_))));
    // OFFS entry that does not match the mip position. The +8 skips the tag name and length, so
    // this is the first offset entry; flipping a bit makes it disagree with the real position.
    let mut b = good.clone();
    let o = b.windows(4).position(|w| w == b"SFFO").unwrap() + 8;
    b[o] ^= 1;
    assert!(matches!(paa::read(&b), Err(Error::Paa(_))));
}

// --- DXT blocks --------------------------------------------------------------------------------

#[test]
fn dxt_flat_colour_block_decodes_close() {
    use crate::dxt::{decode_image, encode_image, Format};
    for fmt in [Format::Dxt1 { punch: false }, Format::Dxt5] {
        let rgba: Vec<u8> = (0..16).flat_map(|_| [200, 90, 30, 255]).collect();
        let data = encode_image(&rgba, 4, 4, fmt);
        let out = decode_image(&data, 4, 4, fmt == Format::Dxt5).unwrap();
        for p in out.chunks(4) {
            assert!(
                (p[0] as i32 - 200).abs() <= 3
                    && (p[1] as i32 - 90).abs() <= 3
                    && (p[2] as i32 - 30).abs() <= 3,
                "{p:?}"
            );
            assert_eq!(p[3], 255);
        }
    }
}

#[test]
fn dxt1_punch_through_keeps_transparent_texels() {
    use crate::dxt::{decode_image, encode_image, Format};
    let rgba: Vec<u8> = (0..16)
        .flat_map(|i| {
            if i % 2 == 0 {
                [0, 0, 0, 0]
            } else {
                [40, 160, 220, 255]
            }
        })
        .collect();
    let data = encode_image(&rgba, 4, 4, Format::Dxt1 { punch: true });
    let out = decode_image(&data, 4, 4, false).unwrap();
    for (i, p) in out.chunks(4).enumerate() {
        assert_eq!(p[3], if i % 2 == 0 { 0 } else { 255 }, "texel {i}");
    }
}

#[test]
fn dxt5_alpha_extremes_survive() {
    use crate::dxt::{decode_image, encode_image, Format};
    let rgba: Vec<u8> = (0..16)
        .flat_map(|i| [10, 10, 10, if i < 8 { 0 } else { 255 }])
        .collect();
    let data = encode_image(&rgba, 4, 4, Format::Dxt5);
    let out = decode_image(&data, 4, 4, true).unwrap();
    for (i, p) in out.chunks(4).enumerate() {
        assert_eq!(p[3], if i < 8 { 0 } else { 255 }, "texel {i}");
    }
}

#[test]
fn decode_image_rejects_bad_geometry() {
    use crate::dxt::decode_image;
    assert!(decode_image(&[0; 8], 4, 4, false).is_ok());
    assert!(decode_image(&[0; 8], 0, 4, false).is_err());
    assert!(decode_image(&[0; 8], 6, 4, false).is_err());
    assert!(decode_image(&[0; 8], 4, 4, true).is_err()); // DXT5 needs 16 bytes
    assert!(decode_image(&[0; 9], 4, 4, false).is_err());
    assert!(decode_image(&[], usize::MAX - 3, 4, false).is_err());
}

// --- PNG variants ------------------------------------------------------------------------------

fn png_with(
    name: &str,
    w: u32,
    h: u32,
    color: png::ColorType,
    depth: png::BitDepth,
    data: &[u8],
    pal: Option<&[u8]>,
) -> Image {
    let path =
        std::env::temp_dir().join(format!("paa-proto-test-{}-{name}.png", std::process::id()));
    {
        let mut enc = png::Encoder::new(std::fs::File::create(&path).unwrap(), w, h);
        enc.set_color(color);
        enc.set_depth(depth);
        if let Some(p) = pal {
            enc.set_palette(p.to_vec());
        }
        let mut wr = enc.write_header().unwrap();
        wr.write_image_data(data).unwrap();
    }
    let r = img::load_png(&path);
    let _ = std::fs::remove_file(&path);
    r.unwrap()
}

#[test]
fn load_png_colour_types_normalise_to_rgba() {
    use png::{BitDepth::*, ColorType::*};
    let i = png_with("rgba", 2, 1, Rgba, Eight, &[1, 2, 3, 4, 5, 6, 7, 8], None);
    assert_eq!(i.rgba, [1, 2, 3, 4, 5, 6, 7, 8]);
    let i = png_with("rgb", 2, 1, Rgb, Eight, &[1, 2, 3, 4, 5, 6], None);
    assert_eq!(i.rgba, [1, 2, 3, 255, 4, 5, 6, 255]);
    let i = png_with("grey", 2, 1, Grayscale, Eight, &[9, 200], None);
    assert_eq!(i.rgba, [9, 9, 9, 255, 200, 200, 200, 255]);
    let i = png_with("greya", 1, 1, GrayscaleAlpha, Eight, &[9, 77], None);
    assert_eq!(i.rgba, [9, 9, 9, 77]);
    let i = png_with(
        "idx",
        2,
        1,
        Indexed,
        Eight,
        &[1, 0],
        Some(&[10, 11, 12, 20, 21, 22]),
    );
    assert_eq!(i.rgba, [20, 21, 22, 255, 10, 11, 12, 255]);
    // 16-bit keeps the high byte
    let i = png_with(
        "rgb16",
        1,
        1,
        Rgb,
        Sixteen,
        &[0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc],
        None,
    );
    assert_eq!(i.rgba, [0x12, 0x56, 0x9a, 255]);
}

#[test]
fn load_png_missing_file_is_io_error() {
    let e = img::load_png(std::path::Path::new("definitely/not/here.png"))
        .err()
        .unwrap();
    assert!(matches!(e, Error::Io { .. }));
}

#[test]
fn load_png_truncated_data_is_error() {
    let mut b = header_only_png(8, 8);
    b.truncate(40);
    assert!(load_from_bytes("trunc", &b).is_err());
    assert!(load_from_bytes("junk", b"not a png at all").is_err());
}

// --- files end to end -------------------------------------------------------------------------

#[test]
fn encode_file_reports_io() {
    let dir = std::env::temp_dir();
    let e = encode::encode_file(
        &dir.join("paa-proto-missing_co.png"),
        &dir.join("x.paa"),
        None,
    )
    .unwrap_err();
    assert!(matches!(e, Error::Io { .. }), "{e}");
}

#[test]
fn error_text_is_short_and_has_no_debug_syntax() {
    let e = Error::MipTooLarge {
        w: 4096,
        h: 4096,
        len: 16_777_216,
    };
    assert!(!e.to_string().contains('{'));
    let e = Error::io(
        std::path::Path::new("a.png"),
        std::io::Error::from(std::io::ErrorKind::NotFound),
    );
    assert!(e.to_string().starts_with("a.png: "));
}
