//! Extra encoder tests. Tests only: nothing here changes behaviour.
//!
//! Two kinds of test live here:
//! * Tests that close gaps the older suite left: each fails if the code under test is broken in the way it names.
//! * The PNG-variant equivalence table. It writes each PNG variant byte by byte (bit depth,
//!   colour type, tRNS, interlace and colour chunks exact, which the `png` crate's own encoder
//!   cannot do) and proves the encoder treats it as the plain 8-bit RGBA of the same pixels.
//!
//! Inputs are generated, no fixtures. Files go to the temp dir under a process-unique name.

use crate::encode;
use crate::error::Error;
use crate::img::{self, Image};
use crate::paa::{self, Mip, Paa};
use crate::tests::{blank, header_only_png, rule, solid};
use std::path::PathBuf;

// --- a PNG writer that controls every field -------------------------------------------------------

/// A PNG chunk to write: its four-letter name and its data.
type Chunk = ([u8; 4], Vec<u8>);

/// Everything that makes one PNG variant. `samples` is row-major, channels interleaved, every
/// value already at `depth` bits (palette indices for colour type 3).
#[derive(Clone, Default)]
struct Png {
    w: usize,
    h: usize,
    depth: u8,
    /// PNG colour type: 0 grey, 2 RGB, 3 indexed, 4 grey+alpha, 6 RGBA.
    ctype: u8,
    interlace: bool,
    plte: Vec<u8>,
    trns: Vec<u8>,
    /// Ancillary chunks written between IHDR and the palette/IDAT (gAMA, sRGB, iCCP, cHRM).
    pre: Vec<Chunk>,
    samples: Vec<u16>,
}

fn channels(ctype: u8) -> usize {
    match ctype {
        0 | 3 => 1,
        2 => 3,
        4 => 2,
        _ => 4,
    }
}

/// zlib stream of stored (uncompressed) deflate blocks: valid for any inflater, no compressor needed.
fn zlib_stored(data: &[u8]) -> Vec<u8> {
    let mut out = vec![0x78, 0x01];
    let blocks: Vec<&[u8]> = if data.is_empty() {
        vec![&[][..]]
    } else {
        data.chunks(65_535).collect()
    };
    for (i, b) in blocks.iter().enumerate() {
        out.push(u8::from(i + 1 == blocks.len())); // BFINAL, BTYPE 00
        out.extend_from_slice(&(b.len() as u16).to_le_bytes());
        out.extend_from_slice(&(!(b.len() as u16)).to_le_bytes());
        out.extend_from_slice(b);
    }
    let (mut a, mut s) = (1u32, 0u32);
    for &v in data {
        a = (a + v as u32) % 65_521;
        s = (s + a) % 65_521;
    }
    out.extend_from_slice(&((s << 16) | a).to_be_bytes());
    out
}

/// One scanline's samples packed to bytes: MSB first, rows padded to a whole byte.
fn pack_row(samples: &[u16], depth: u8) -> Vec<u8> {
    match depth {
        16 => samples.iter().flat_map(|v| v.to_be_bytes()).collect(),
        8 => samples.iter().map(|&v| v as u8).collect(),
        d => {
            let per = 8 / d as usize;
            samples
                .chunks(per)
                .map(|c| {
                    c.iter().enumerate().fold(0u8, |b, (i, &v)| {
                        b | (v as u8) << (8 - d as usize * (i + 1))
                    })
                })
                .collect()
        }
    }
}

/// The seven Adam7 passes as (x start, y start, x step, y step).
const ADAM7: [(usize, usize, usize, usize); 7] = [
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
];

fn png_bytes(p: &Png) -> Vec<u8> {
    let ch = channels(p.ctype);
    assert_eq!(p.samples.len(), p.w * p.h * ch);
    let px = |x: usize, y: usize| &p.samples[(y * p.w + x) * ch..][..ch];
    let mut raw = Vec::new();
    if p.interlace {
        for (x0, y0, dx, dy) in ADAM7 {
            let xs: Vec<usize> = (x0..p.w).step_by(dx).collect();
            for y in (y0..p.h).step_by(dy) {
                if xs.is_empty() {
                    continue;
                }
                raw.push(0); // filter type None
                let row: Vec<u16> = xs.iter().flat_map(|&x| px(x, y).iter().copied()).collect();
                raw.extend(pack_row(&row, p.depth));
            }
        }
    } else {
        for y in 0..p.h {
            raw.push(0);
            raw.extend(pack_row(
                &p.samples[y * p.w * ch..(y + 1) * p.w * ch],
                p.depth,
            ));
        }
    }
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&(p.w as u32).to_be_bytes());
    ihdr.extend_from_slice(&(p.h as u32).to_be_bytes());
    ihdr.extend_from_slice(&[p.depth, p.ctype, 0, 0, u8::from(p.interlace)]);
    crate::tests::chunk(&mut out, b"IHDR", &ihdr);
    for (name, data) in &p.pre {
        crate::tests::chunk(&mut out, name, data);
    }
    if !p.plte.is_empty() {
        crate::tests::chunk(&mut out, b"PLTE", &p.plte);
    }
    if !p.trns.is_empty() {
        crate::tests::chunk(&mut out, b"tRNS", &p.trns);
    }
    crate::tests::chunk(&mut out, b"IDAT", &zlib_stored(&raw));
    crate::tests::chunk(&mut out, b"IEND", &[]);
    out
}

/// The 8-bit RGBA the PNG standard says this file holds. 16-bit samples map to `v >> 8` (the high
/// byte): that is what `img.rs` documents and the `png` crate's STRIP_16 does, so the comparison
/// below is byte-exact, not "within 1 level". Sub-8-bit grey scales to 0..255 by `v * 255 / max`.
fn ideal(p: &Png) -> Vec<u8> {
    let ch = channels(p.ctype);
    let to8 = |v: u16| {
        if p.depth == 16 {
            (v >> 8) as u8
        } else {
            (v as u32 * 255 / ((1u32 << p.depth) - 1)) as u8
        }
    };
    // The colour key (grey and RGB only; for indexed images tRNS is a list of alphas instead).
    let key: Vec<u16> = if p.ctype == 3 {
        vec![]
    } else {
        p.trns
            .chunks(2)
            .map(|c| u16::from_be_bytes([c[0], c[1]]))
            .collect()
    };
    let mut out = Vec::new();
    for s in p.samples.chunks(ch) {
        let px: [u8; 4] = match p.ctype {
            0 => [
                to8(s[0]),
                to8(s[0]),
                to8(s[0]),
                if key == [s[0]] { 0 } else { 255 },
            ],
            2 => [
                to8(s[0]),
                to8(s[1]),
                to8(s[2]),
                if key == s { 0 } else { 255 },
            ],
            3 => {
                let i = s[0] as usize;
                [
                    p.plte[i * 3],
                    p.plte[i * 3 + 1],
                    p.plte[i * 3 + 2],
                    p.trns.get(i).copied().unwrap_or(255),
                ]
            }
            4 => [to8(s[0]), to8(s[0]), to8(s[0]), to8(s[1])],
            _ => [to8(s[0]), to8(s[1]), to8(s[2]), to8(s[3])],
        };
        out.extend_from_slice(&px);
    }
    out
}

/// A variant with a deterministic sample pattern. `key`: add a tRNS colour key taken from pixel
/// (3, 2), so some pixels are keyed. `pal_alpha`: add a tRNS alpha list one entry shorter than
/// the palette (indexed only).
fn variant(ctype: u8, depth: u8, interlace: bool, key: bool, pal_alpha: bool) -> Png {
    let (w, h) = (16usize, 16usize);
    let ch = channels(ctype);
    let levels = 1u32 << depth;
    let mut samples = Vec::new();
    for y in 0..h {
        for x in 0..w {
            for c in 0..ch {
                let v = (x * 37 + y * 91 + c * 53 + x * y * 3) as u32;
                samples.push(if depth == 16 {
                    ((v * 1013 + 7) % 65_536) as u16
                } else {
                    (v % levels) as u16
                });
            }
        }
    }
    let mut p = Png {
        w,
        h,
        depth,
        ctype,
        interlace,
        samples,
        ..Png::default()
    };
    if ctype == 3 {
        let n = (levels as usize).min(256);
        for i in 0..n {
            p.plte.extend([
                ((i * 37 + 11) % 256) as u8,
                ((i * 91 + 5) % 256) as u8,
                ((i * 53 + 200) % 256) as u8,
            ]);
        }
        if pal_alpha {
            p.trns = (0..(n * 3 / 4).max(1))
                .map(|i| [0, 255, 128, 64][i % 4])
                .collect();
        }
    }
    if key {
        let at = (2 * w + 3) * ch;
        p.trns = p.samples[at..at + ch]
            .iter()
            .flat_map(|v| v.to_be_bytes())
            .collect();
    }
    p
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("paa-proto-gaps-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// PAA bytes of the PNG file `bytes` as `<case>_dxt5.png`. `_dxt5` is DXT5 with `dynRange` 0, so
/// soft alpha is encoded rather than refused, and it has no swizzle: the bytes depend on the
/// pixels alone.
fn paa_of_png(case: &str, bytes: &[u8]) -> Vec<u8> {
    let dir = scratch(case);
    let png = dir.join(format!("{case}_dxt5.png"));
    std::fs::write(&png, bytes).unwrap();
    let out = dir.join("x.paa");
    let r = encode::encode_file(&png, &out, None);
    let got = r.map(|()| std::fs::read(&out).unwrap());
    let _ = std::fs::remove_dir_all(&dir);
    got.unwrap_or_else(|e| panic!("{case}: {e}"))
}

/// The PAA of the plain 8-bit RGBA PNG made from `p`'s ideal pixels.
fn plain_paa(case: &str, p: &Png) -> Vec<u8> {
    let plain = Png {
        depth: 8,
        ctype: 6,
        interlace: false,
        plte: vec![],
        trns: vec![],
        pre: vec![],
        samples: ideal(p).iter().map(|&v| v as u16).collect(),
        ..p.clone()
    };
    paa_of_png(&format!("{case}-plain"), &png_bytes(&plain))
}

// Every PNG variant the encoder accepts gives the
// same PAA as the plain 8-bit RGBA of the same pixels. ImageToPAA mangles or rejects most of these
// (grey, 16-bit, indexed, colour key); the encoder must not. 16-bit is pinned to `v >> 8`.
#[test]
fn png_variants_encode_like_their_8_bit_expansion() {
    let mut cases: Vec<(String, Png)> = Vec::new();
    for d in [1, 2, 4, 8, 16] {
        cases.push((format!("gray{d}"), variant(0, d, false, false, false)));
    }
    for d in [2, 8, 16] {
        cases.push((format!("gray{d}-key"), variant(0, d, false, true, false)));
    }
    for d in [8, 16] {
        cases.push((format!("graya{d}"), variant(4, d, false, false, false)));
        cases.push((format!("rgb{d}"), variant(2, d, false, false, false)));
        cases.push((format!("rgb{d}-key"), variant(2, d, false, true, false)));
        cases.push((format!("rgba{d}"), variant(6, d, false, false, false)));
    }
    for d in [1, 2, 4, 8] {
        cases.push((format!("pal{d}"), variant(3, d, false, false, false)));
        cases.push((format!("pal{d}-trns"), variant(3, d, false, false, true)));
    }
    // Adam7: the same pixels in seven passes, every colour type that matters.
    for (name, ct, d, key, pal_alpha) in [
        ("rgb8", 2, 8, false, false),
        ("rgba8", 6, 8, false, false),
        ("rgb16", 2, 16, false, false),
        ("gray8", 0, 8, false, false),
        ("gray2", 0, 2, false, false),
        ("pal4", 3, 4, false, false),
        ("pal4-trns", 3, 4, false, true),
        ("gray8-key", 0, 8, true, false),
    ] {
        cases.push((
            format!("{name}-adam7"),
            variant(ct, d, true, key, pal_alpha),
        ));
    }
    let mut bad = Vec::new();
    for (name, p) in &cases {
        let got = paa_of_png(name, &png_bytes(p));
        if got != plain_paa(name, p) {
            bad.push(name.as_str());
        }
    }
    assert!(
        bad.is_empty(),
        "PAA differs from the plain 8-bit expansion: {bad:?}"
    );
    // The table is not vacuous: the colour-key cases really hold transparent and opaque texels.
    let a: Vec<u8> = ideal(&variant(2, 8, false, true, false))
        .chunks(4)
        .map(|p| p[3])
        .collect();
    assert!(a.contains(&0) && a.contains(&255));
}

// The loader alone, at odd sizes and across the Adam7 passes (a width of 13 leaves some passes
// with fewer pixels than the pass width, a height of 3 leaves some passes empty).
#[test]
fn load_png_matches_the_ideal_pixels_at_odd_sizes() {
    for (ct, d, key, pal_alpha, interlace) in [
        (2, 8, false, false, true),
        (0, 4, false, false, true),
        (3, 2, false, true, true),
        (6, 16, false, false, true),
        (0, 8, true, false, true),
        (2, 8, false, false, false),
    ] {
        for (w, h) in [(13usize, 3usize), (1, 1), (5, 9)] {
            let mut p = variant(ct, d, interlace, key, pal_alpha);
            let ch = channels(ct);
            let levels = 1u32 << d;
            p.samples = (0..w * h * ch)
                .map(|i| (((i * 7919) as u32) % levels.min(65_536)) as u16)
                .collect();
            p.w = w;
            p.h = h;
            if key {
                p.trns = p.samples[..ch]
                    .iter()
                    .flat_map(|v| v.to_be_bytes())
                    .collect();
            }
            let path = scratch("odd").join("v.png");
            std::fs::write(&path, png_bytes(&p)).unwrap();
            let got = img::load_png(&path).unwrap_or_else(|e| panic!("{ct}/{d} {w}x{h}: {e}"));
            let _ = std::fs::remove_dir_all(path.parent().unwrap());
            assert_eq!((got.w, got.h), (w, h));
            assert_eq!(
                got.rgba,
                ideal(&p),
                "colour type {ct} depth {d} {w}x{h} interlace {interlace}"
            );
        }
    }
}

// Colour-space chunks change nothing. Neither the loader nor
// ImageToPAA applies a gamma or a profile; the pixels go through as stored.
#[test]
fn colour_space_chunks_do_not_change_the_pixels() {
    let plain = variant(2, 8, false, false, false);
    let want = paa_of_png("chunks-plain", &png_bytes(&plain));
    let gama = |g: u32| (*b"gAMA", g.to_be_bytes().to_vec());
    let chrm_709 = [31270u32, 32900, 64000, 33000, 30000, 60000, 15000, 6000]
        .iter()
        .flat_map(|v| v.to_be_bytes())
        .collect::<Vec<u8>>();
    let chrm_adobe = [31270u32, 32900, 64000, 33000, 21000, 71000, 15000, 6000]
        .iter()
        .flat_map(|v| v.to_be_bytes())
        .collect::<Vec<u8>>();
    // iCCP: profile name, NUL, compression method 0, zlib stream. The profile bytes are junk (the
    // decoders must not apply or validate it, which is what is being pinned); a real v2 matrix
    // profile gave the same result in the scan.
    let mut iccp = b"Adobe RGB\0\0".to_vec();
    iccp.extend(zlib_stored(&[0x5A; 128]));
    let sets: Vec<(&str, Vec<Chunk>)> = vec![
        ("gama-0.45455", vec![gama(45_455)]),
        ("gama-1.0", vec![gama(100_000)]),
        ("gama-1.8", vec![gama(55_556)]),
        ("srgb", vec![(*b"sRGB", vec![0])]),
        ("iccp", vec![(*b"iCCP", iccp)]),
        ("chrm-709", vec![(*b"cHRM", chrm_709.clone())]),
        ("chrm-adobe", vec![(*b"cHRM", chrm_adobe)]),
        ("gama+chrm", vec![gama(45_455), (*b"cHRM", chrm_709)]),
    ];
    for (name, pre) in sets {
        let p = Png {
            pre,
            ..plain.clone()
        };
        assert_eq!(paa_of_png(name, &png_bytes(&p)), want, "{name}");
    }
    // 16-bit with gAMA, as in the scan.
    let wide = variant(2, 16, false, false, false);
    let with = Png {
        pre: vec![gama(45_455)],
        ..wide.clone()
    };
    assert_eq!(
        paa_of_png("chunks16", &png_bytes(&with)),
        paa_of_png("chunks16-plain", &png_bytes(&wide))
    );
}

// --- palette hazards -------------------------------------------------------

// A palette index past the end of the palette is not an error for the loader (Loki converts it,
// ImageToPAA rejects it). Pinned so a change is noticed; it must not panic.
#[test]
fn out_of_range_palette_index_does_not_panic() {
    let mut p = variant(3, 4, false, false, false);
    p.plte.truncate(4 * 3); // 4 entries, indices run to 15
    let path = scratch("oob").join("v.png");
    std::fs::write(&path, png_bytes(&p)).unwrap();
    let r = std::panic::catch_unwind(|| img::load_png(&path));
    let _ = std::fs::remove_dir_all(path.parent().unwrap());
    let r = r.expect("load_png panicked");
    assert!(
        r.is_ok() || matches!(r, Err(Error::Png(_))),
        "index past the palette"
    );
}

// A PLTE chunk whose length is not a multiple of 3 panics inside the png crate (0.17.16,
// transform/palette.rs) even when every index is 0. The fix rejects it in `load_png`.
#[test]
fn plte_length_not_a_multiple_of_3_is_a_png_error_not_a_panic() {
    let mut p = variant(3, 8, false, false, false);
    p.samples.iter_mut().for_each(|v| *v = 0);
    p.plte = vec![9; 25];
    let path = scratch("plte25").join("v.png");
    std::fs::write(&path, png_bytes(&p)).unwrap();
    let r = std::panic::catch_unwind(|| img::load_png(&path));
    let _ = std::fs::remove_dir_all(path.parent().unwrap());
    assert!(
        matches!(r, Ok(Err(Error::Png(_)))),
        "PLTE of 25 bytes: {:?}",
        r.as_ref().map(|r| r.as_ref().err().map(|e| e.to_string()))
    );
}

// --- size guards ---------------------------------------------------------

#[test]
fn encode_accepts_the_largest_side_and_refuses_one_octave_more_with_a_clear_message() {
    // MAX_DIM (4096) is accepted on either side of a non-square image ...
    for (w, h) in [(4096, 4), (4, 4096)] {
        assert!(encode::encode(blank(w, h), rule("dxt5")).is_ok(), "{w}x{h}");
    }
    // ... and 8192 on either side is refused. The buffer is full length, so only the size guard
    // can be what refuses it (a short buffer is refused by a different check, which hid the guard).
    for (w, h) in [(8192, 4), (4, 8192)] {
        let e = encode::encode(blank(w, h), rule("dxt5"))
            .err()
            .unwrap_or_else(|| panic!("{w}x{h} was accepted"));
        assert!(
            matches!(e, Error::Size(_)) && e.to_string().contains("exceeds 4096"),
            "{w}x{h}: {e}"
        );
    }
}

#[test]
fn encode_refuses_sides_below_4_and_non_square_odd_sizes_with_the_size_message() {
    for (w, h) in [
        (1, 1),
        (2, 2),
        (1, 4),
        (4, 1),
        (2, 8),
        (8, 2),
        (3, 4),
        (4, 6),
        (12, 8),
    ] {
        let e = encode::encode(blank(w, h), rule("dxt5"))
            .err()
            .unwrap_or_else(|| panic!("{w}x{h} was accepted"));
        assert!(matches!(e, Error::Size(_)), "{w}x{h}: {e}");
        assert!(e.to_string().contains(&format!("{w}x{h}")), "{e}");
    }
    // Non-square power-of-two sizes are fine; the chain of a DXT image stops when a side is under 8.
    for (w, h, mips) in [(4, 16, 1), (16, 4, 1), (8, 32, 2), (32, 8, 2)] {
        let p = paa::read(&encode::encode(blank(w, h), rule("dxt5")).unwrap()).unwrap();
        assert_eq!(p.mips.len(), mips, "{w}x{h}");
    }
}

// The PNG header guard: up to 16384 on a side and 8192x8192 pixels in all pass it; one more
// on a side or in pixel count does not. The text is what tells the refusals apart (a header-only
// file fails later for another reason), so it is asserted.
#[test]
fn png_header_size_guard_is_the_load_limit() {
    let msg = |w: u32, h: u32| load_png_msg(&header_only_png(w, h));
    for (w, h) in [
        (16385, 4),
        (4, 16385),
        (32768, 4),
        (8193, 8192),
        (16384, 8192),
        (20000, 20000),
    ] {
        assert!(msg(w, h).contains(&format!("PNG size {w}x{h} is too big to load (at most 16384 on a side and 8192x8192 pixels in all): scale it to 8192 or less in your image editor")), "{w}x{h}: {}", msg(w, h));
    }
    for (w, h) in [
        (16384, 4),
        (4, 16384),
        (16384, 4096),
        (8192, 8192),
        (4096, 4096),
        (1, 1),
    ] {
        assert!(
            !msg(w, h).contains("PNG size"),
            "{w}x{h} passed the guard but: {}",
            msg(w, h)
        );
    }
    // And a real 16384-wide PNG loads in full.
    let p = Png {
        w: 16384,
        h: 4,
        depth: 8,
        ctype: 6,
        samples: vec![7; 16384 * 4 * 4],
        ..Png::default()
    };
    let path = scratch("w16384").join("v.png");
    std::fs::write(&path, png_bytes(&p)).unwrap();
    let got = img::load_png(&path).unwrap();
    let _ = std::fs::remove_dir_all(path.parent().unwrap());
    assert_eq!((got.w, got.h, got.rgba.len()), (16384, 4, 16384 * 4 * 4));
}

// A PNG over 4096 is halved to 4096 (the oracle's cap) with the
// rule's own filter, so the file equals encoding `limit_size(.., 4096)`; a max size below that still wins.
#[test]
fn encode_file_halves_an_over_4096_png_with_the_rules_filter() {
    let (w, h) = (8192usize, 64usize);
    let samples: Vec<u16> = (0..w * h * 3)
        .map(|i| ((i * 7 + i / 5000) % 251) as u16)
        .collect();
    let rgba: Vec<u8> = samples
        .chunks(3)
        .flat_map(|p| [p[0] as u8, p[1] as u8, p[2] as u8, 255])
        .collect();
    let dir = scratch("over4096");
    let png = dir.join("big_co.png");
    std::fs::write(
        &png,
        png_bytes(&Png {
            w,
            h,
            depth: 8,
            ctype: 2,
            samples,
            ..Png::default()
        }),
    )
    .unwrap();
    let out = dir.join("big_co.paa");
    crate::encode_file(&png, &out, None).unwrap();
    let want = encode::encode(
        encode::limit_size(Image { w, h, rgba }, 4096, rule("co")).unwrap(),
        rule("co"),
    )
    .unwrap();
    assert!(
        std::fs::read(&out).unwrap() == want,
        "default cap is the 4096 halving"
    );
    let top = |bytes: &[u8]| {
        let m = &paa::read(bytes).unwrap().mips[0];
        (m.w, m.h)
    };
    assert_eq!(top(&want), (4096, 32));
    crate::encode_file(&png, &out, Some(1024)).unwrap();
    assert_eq!(
        top(&std::fs::read(&out).unwrap()),
        (1024, 8),
        "the user's max size applies on top"
    );
    crate::encode_file(&png, &out, Some(8192)).unwrap();
    assert_eq!(
        top(&std::fs::read(&out).unwrap()),
        (4096, 32),
        "a max size above 4096 does not lift the cap"
    );
    // A side that is not a power of two cannot be halved to a valid size: the message says so.
    let odd = dir.join("odd_co.png");
    std::fs::write(
        &odd,
        png_bytes(&Png {
            w: 5000,
            h: 8,
            depth: 8,
            ctype: 2,
            samples: vec![9; 5000 * 8 * 3],
            ..Png::default()
        }),
    )
    .unwrap();
    let e = crate::encode_file(&odd, &out, None).unwrap_err();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        matches!(e, Error::Size(_)) && e.to_string().contains("power-of-two"),
        "{e}"
    );
}

/// The loader's error text for `bytes` ("" when it loads).
fn load_png_msg(bytes: &[u8]) -> String {
    match crate::tests::load_from_bytes("guard", bytes) {
        Ok(()) => String::new(),
        Err(e) => e.to_string(),
    }
}

// --- sides below 4 -----------------------------------------------------------------

// ImageToPAA converts these for the non-DXT classes; Loki used to refuse with a ValueError and no hand-over,
// and the text blamed a power of two for a 256x1 strip. fixed in 0.3.1: non-DXT classes are encoded (tests_small.rs).
#[test]
fn tiny_sides_are_handed_over_or_encoded_never_a_size_error() {
    for suffix in ["raw", "4444", "gs", ""] {
        for (w, h) in [(1, 1), (2, 2), (1, 256), (256, 1)] {
            match encode::encode(blank(w, h), rule(suffix)) {
                Ok(_) | Err(Error::Unsupported { .. }) => {}
                Err(e) => panic!("_{suffix} {w}x{h}: {e}"),
            }
        }
    }
}

// --- the max-size downscale is the rule's mip filter -----------------------------------

/// 128x128 images where the byte box and the oracle's chain filter disagree: a checker of
/// black and white, and an opaque image with a transparent border row.
fn filter_probe(kind: usize) -> Image {
    let n = 128;
    let mut rgba = Vec::new();
    for y in 0..n {
        for x in 0..n {
            rgba.extend_from_slice(&match kind {
                0 => {
                    if (x + y) % 2 == 0 {
                        [255, 255, 255, 255]
                    } else {
                        [0, 0, 0, 255]
                    }
                }
                _ => [
                    (x * 2) as u8,
                    (y * 2) as u8,
                    90,
                    if y < 2 || x < 2 { 0 } else { 255 },
                ],
            });
        }
    }
    Image { w: n, h: n, rgba }
}

#[test]
fn limit_size_is_the_rules_mip_1() {
    // `_co` checker, `_nohq` alpha border, `_nsex` alpha border: ours was 13.7, 27.8 and 44.6 dB
    // from ImageToPAA's mip 1.
    for (suffix, kind) in [("co", 0), ("nohq", 1), ("nsex", 1)] {
        let r = rule(suffix);
        let want = crate::mipfilter::next_mip(&filter_probe(kind), r.mip_filter, r.linear_mips);
        let got = encode::limit_size(filter_probe(kind), 64, r).unwrap();
        assert_eq!((got.w, got.h), (64, 64));
        assert!(
            got.rgba == want.rgba,
            "_{suffix}: max-size 64 differs from the rule's mip 1"
        );
    }
}

// A binary-alpha `_ca` (alpha 0 or 255, holes on 2x2 boundaries) keeps binary alpha through the
// rule's mip filter (alpha is a plain mean, and each quad is all 0 or all 255), so it is still
// encoded here after max-size, with the binary alpha it started with.
#[test]
fn binary_alpha_ca_stays_binary_after_max_size() {
    let n = 128;
    let mut rgba = Vec::new();
    for y in 0..n {
        for x in 0..n {
            let a = if (x / 4 + y / 4) % 3 == 0 { 0 } else { 255 };
            rgba.extend_from_slice(&[(x * 2) as u8, (y * 2) as u8, 90, a]);
        }
    }
    let r = rule("ca");
    let got = encode::limit_size(Image { w: n, h: n, rgba }, 64, r).unwrap();
    assert_eq!((got.w, got.h), (64, 64));
    assert!(
        got.rgba.chunks_exact(4).all(|p| p[3] == 0 || p[3] == 255),
        "alpha became soft"
    );
    assert!(
        got.rgba.chunks_exact(4).any(|p| p[3] == 0),
        "the test image has holes"
    );
    assert!(encode::encode(got, r).is_ok());
}

// A binary-alpha `_ca` whose hard edge is NOT on the 2x2 grid (a 1-px-wide
// hole at an odd column, so each quad straddling it averages to alpha 127) turns soft after the mip
// filter. Soft alpha is encoded here now (the AVGC of soft alpha is reproduced), so this file converts in
// Loki instead of being handed to ImageToPAA: FLAG 1 (soft alpha, DXT5), AVGC alpha below 255.
#[test]
fn off_grid_binary_alpha_ca_converts_in_loki_after_max_size() {
    let n = 128;
    let mut rgba = Vec::new();
    for y in 0..n {
        for x in 0..n {
            let a = if x == 63 { 0 } else { 255 };
            rgba.extend_from_slice(&[(x * 2) as u8, (y * 2) as u8, 90, a]);
        }
    }
    let r = rule("ca");
    let src = Image { w: n, h: n, rgba };
    let got = encode::limit_size(src, 64, r).unwrap();
    assert_eq!((got.w, got.h), (64, 64));
    assert!(
        got.rgba.chunks_exact(4).any(|p| p[3] != 0 && p[3] != 255),
        "alpha became soft"
    );
    let p = paa::read(&encode::encode(got, r).unwrap()).unwrap();
    assert_eq!(p.ty, paa::TYPE_DXT5);
    let flag = p
        .tags
        .iter()
        .find(|t| t.name == "FLAG")
        .expect("a FLAG tag");
    assert_eq!(flag.data, [1, 0, 0, 0], "soft alpha");
    assert!(p.tags[0].data[3] < 255, "the AVGC alpha counts the hole");
}

// --- the alpha-weighted chain of a flat colour ---------------------------------------

// The alpha-weighted mean of equal colours is that colour, whatever the weights. The chain used
// floating point weights and floored R and G, so it was one level low on about 13 percent of
// quads. It was replaced with the rule fitted to ImageToPAA on flat and
// non-flat data: the exact weighted mean, half to even.
#[test]
fn alpha_weighted_chain_keeps_a_flat_colour_exactly() {
    use crate::rules::Filter;
    let mut s = 5u32;
    let mut rgba = Vec::new();
    for _ in 0..64 * 64 {
        s = s.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        rgba.extend_from_slice(&[200, 100, 50, 1 + (s >> 24) as u8 % 254]);
    }
    let img = Image { w: 64, h: 64, rgba };
    let m = crate::mipfilter::next_mip(&img, Some(Filter::NormalizeNormalMapAlpha), false);
    let off = m
        .rgba
        .chunks(4)
        .filter(|p| p[..3] != [200, 100, 50])
        .count();
    assert_eq!(
        off,
        0,
        "{off} of {} texels are not the flat colour",
        m.w * m.h
    );
}

// Ties of the weighted mean go to the even value (fitted: with constant alpha 128 the oracle
// output matched half-to-even on every stored level of noise, where half-up and floor missed 2 to
// 6 percent); a quad whose alphas are all 255 (or all 0) is the plain byte box (R, G truncate, B
// rounds).
#[test]
fn alpha_weighted_ties_go_to_even_and_opaque_quads_use_the_box() {
    use crate::rules::Filter;
    let quad = |a: u8, r: [u8; 4], b: [u8; 4]| Image {
        w: 2,
        h: 2,
        rgba: (0..4).flat_map(|i| [r[i], 0, b[i], a]).collect(),
    };
    let f = Some(Filter::NormalizeNormalMapAlpha);
    // alpha 128 each: R 1,2,1,2 = 1.5 -> 2 (floor would be 1); B 3,3,2,2 = 2.5 -> 2 (round-up would be 3)
    let m = crate::mipfilter::next_mip(&quad(128, [1, 2, 1, 2], [3, 3, 2, 2]), f, false);
    assert_eq!((m.rgba[0], m.rgba[2]), (2, 2));
    // alpha 255 each: plain box, R truncates 1.5 -> 1, B rounds 2.5 -> 3
    let m = crate::mipfilter::next_mip(&quad(255, [1, 2, 1, 2], [3, 3, 2, 2]), f, false);
    assert_eq!((m.rgba[0], m.rgba[2]), (1, 3));
    // a partly transparent quad is weighted: alphas 255, 255, 255, 1 pull toward the heavy texels
    let mut q = quad(255, [10, 10, 10, 200], [0; 4]);
    q.rgba[15] = 1;
    let m = crate::mipfilter::next_mip(&q, f, false);
    assert_eq!(m.rgba[0], 10);
}

// --- helpers out of contract -----------------------------------------------------------

// `Image::check` is the one buffer-length check: exact length passes, short, long and a
// w * h * 4 that overflows usize are `Error::Size`, and `encode` and `limit_size` both use it.
#[test]
fn image_check_accepts_only_an_exact_buffer() {
    let img = |w: usize, h: usize, n: usize| Image {
        w,
        h,
        rgba: vec![0; n],
    };
    assert!(img(2, 3, 24).check().is_ok());
    for bad in [img(2, 3, 23), img(2, 3, 25), img(usize::MAX, 2, 8)] {
        assert!(
            matches!(bad.check(), Err(Error::Size(_))),
            "{}x{} with {} bytes",
            bad.w,
            bad.h,
            bad.rgba.len()
        );
    }
    let rule = rule("co");
    assert!(matches!(
        encode::encode(img(8, 8, 100), rule),
        Err(Error::Size(_))
    ));
    assert!(matches!(
        encode::limit_size(img(64, 64, 100), 16, rule),
        Err(Error::Size(_))
    ));
}

#[test]
fn helpers_on_bad_input_return_an_error_instead_of_panicking() {
    use std::panic::catch_unwind;
    // limit_size with a pixel buffer shorter than w * h * 4.
    let short = || Image {
        w: 64,
        h: 64,
        rgba: vec![0; 100],
    };
    let r = catch_unwind(|| encode::limit_size(short(), 16, rule("co")));
    assert!(
        matches!(r, Ok(Err(Error::Size(_)))),
        "limit_size on a short buffer"
    );
    // dxt::encode_image on a 2x2 image (not a whole block).
    let r = catch_unwind(|| crate::dxt::encode_image(&[0; 16], 2, 2, crate::dxt::Format::Dxt5));
    assert!(
        matches!(r, Ok(ref v) if v.is_empty()),
        "encode_image on 2x2 must give no blocks, not panic"
    );
    // next_mip of a 1x1 image is not a 0x0 image.
    let one = Image {
        w: 1,
        h: 1,
        rgba: vec![9; 4],
    };
    let m = crate::mipfilter::next_mip(&one, None, false);
    assert!(m.w >= 1 && m.h >= 1, "next_mip of 1x1 is {}x{}", m.w, m.h);
}

// --- paa::write validation -------------------------------------------------------------

#[test]
fn paa_write_refuses_shapes_the_reader_cannot_return() {
    let mip = |w: u32, h: u32| Mip {
        w,
        h,
        lzo: false,
        data: vec![0; 8],
    };
    let with = |tags: Vec<paa::Tag>, mips: Vec<Mip>| Paa {
        ty: paa::TYPE_DXT1,
        tags,
        mips,
    };
    let tag = |n: &str| paa::Tag {
        name: n.into(),
        data: vec![],
    };
    assert!(
        paa::write(&with(vec![], vec![mip(8, 8), mip(0, 0), mip(4, 4)])).is_err(),
        "a 0x0 mip would end the chain early"
    );
    assert!(
        paa::write(&with(vec![tag("OFFS")], vec![mip(4, 4)])).is_err(),
        "a user OFFS tag duplicates the real one"
    );
    assert!(
        paa::write(&with(vec![tag("ÄBC")], vec![mip(4, 4)])).is_err(),
        "a non-ASCII tag name does not round-trip"
    );
}

// --- hostile header sizes in the pixel-format decoder -------------------------------------

// decode_mip reserves w * h * 2 bytes before reading any data: an 8-byte stream declaring
// 32767x65535 asks for 4.29 GB (an abort under a small memory cap). The decoder caps the
// reservation by the data length, so this now runs quickly in a few bytes. A reservation of the
// full size would abort or crawl here, which is how a regression shows.
#[test]
fn pixel_decode_of_a_huge_declared_size_fails_fast_without_a_huge_reservation() {
    for ty in [0x1555, 0x4444, 0x8080] {
        let r = crate::decode_mip(ty, &[0u8; 8], 0x7FFF, 0xFFFF);
        assert!(r.is_err(), "{ty:#x}");
    }
}

// The LZO bomb: a stream that expands far past a 4x4 DXT1 header's 8 bytes must be refused
// at the declared size, not built in full. 16 MB of zeros packs to about 100 KB.
#[test]
fn lzo_bomb_is_refused_at_the_declared_size() {
    let packed = lzokay_native::compress(&vec![0u8; 16 << 20]).unwrap();
    assert!(packed.len() < 1 << 20, "{} bytes", packed.len());
    let m = Mip {
        w: 4,
        h: 4,
        lzo: true,
        data: packed,
    };
    let e = crate::mip_raw(&m, false).expect_err("bomb must be an error");
    assert!(e.to_string().contains("lzo decode 4x4"), "{e}");
}

// Small LZO mips: DXT stores whole blocks, so 2x2 and 1x1 are one block (8 bytes DXT1, 16
// DXT5), and a 8x2 mip is two blocks. The old w*h/2 formula said 2, 0 and 4.
#[test]
fn small_lzo_mips_decode_with_the_block_based_size() {
    for (w, h, dxt5, blocks) in [
        (2u32, 2u32, false, 1usize),
        (1, 1, false, 1),
        (2, 2, true, 1),
        (1, 1, true, 1),
        (8, 2, false, 2),
        (8, 2, true, 2),
        (4, 4, false, 1),
        (16, 16, true, 16),
    ] {
        let raw: Vec<u8> = (0..blocks * if dxt5 { 16 } else { 8 })
            .map(|i| i as u8)
            .collect();
        let m = Mip {
            w,
            h,
            lzo: true,
            data: lzokay_native::compress(&raw).unwrap(),
        };
        assert_eq!(
            crate::mip_raw(&m, dxt5).unwrap().as_ref(),
            raw.as_slice(),
            "{w}x{h} dxt5={dxt5}"
        );
    }
    // A stream of the wrong length is still a corruption error.
    let m = Mip {
        w: 2,
        h: 2,
        lzo: true,
        data: lzokay_native::compress(&[1u8; 4]).unwrap(),
    };
    assert!(crate::mip_raw(&m, false).is_err());
}

// An unflagged mip is returned as stored, whatever its size.
#[test]
fn mip_raw_passes_an_unflagged_mip_through() {
    let m = Mip {
        w: 1,
        h: 1,
        lzo: false,
        data: vec![9; 3],
    };
    assert_eq!(crate::mip_raw(&m, true).unwrap().as_ref(), &[9, 9, 9]);
}

// The LZSS reservation is capped by the data, but a valid stream larger than the cap's floor still
// decodes: 4000 bytes of one value pack to far fewer bytes than the output.
#[test]
fn lzss_output_larger_than_the_reservation_cap_still_decodes() {
    let raw = vec![7u8; 4000];
    let packed = crate::argb::lzss_pack(&raw);
    assert_eq!(crate::argb::lzss_unpack(&packed, raw.len()).unwrap(), raw);
}

// --- decoder block order ---------------------------------------

/// Image of flat 4x4 blocks, every block its own colour.
fn block_image(w: usize, h: usize) -> (Vec<u8>, impl Fn(usize, usize) -> [u8; 3]) {
    let colour = |bx: usize, by: usize| {
        [
            (40 + 50 * bx) as u8,
            (30 + 90 * by) as u8,
            (200 - 30 * bx - 20 * by) as u8,
        ]
    };
    let mut rgba = vec![0u8; w * h * 4];
    for y in 0..h {
        for x in 0..w {
            let c = colour(x / 4, y / 4);
            rgba[(y * w + x) * 4..][..4].copy_from_slice(&[c[0], c[1], c[2], 255]);
        }
    }
    (rgba, colour)
}

#[test]
fn decoder_places_every_block_where_the_encoder_put_it() {
    use crate::dxt::{decode_image, encode_image, Format};
    // 8x8 is the four-quadrant case; 16x8 and 8x16 have a different block stride in each direction,
    // which a transposed index would show.
    for (w, h) in [(8, 8), (16, 8), (8, 16), (12, 4)] {
        let (rgba, colour) = block_image(w, h);
        for fmt in [Format::Dxt1 { punch: false }, Format::Dxt5] {
            let out =
                decode_image(&encode_image(&rgba, w, h, fmt), w, h, fmt == Format::Dxt5).unwrap();
            for y in 0..h {
                for x in 0..w {
                    let (want, got) = (colour(x / 4, y / 4), &out[(y * w + x) * 4..][..3]);
                    assert!(
                        want.iter().zip(got).all(|(&a, &b)| a.abs_diff(b) <= 6),
                        "{fmt:?} {w}x{h} texel ({x},{y}): want {want:?}, got {got:?}"
                    );
                }
            }
        }
    }
}

// --- LZSS -------------------------------------

// A match that reaches before the start of the output reads the window's initial fill, spaces
// (0x20). Real ImageToPAA files do this (2 of 810 mips) and ImageToPAA's own reader
// agrees on this stream, so the byte is part of the format and an "error on pre-window match" would
// break real files. Stream: flags 0b10, a match of distance 1 length 3 (three 0x20), literal 0xAA, then the
// checksum 0x20*3 + (0xAA as i8) = 10.
#[test]
fn lzss_pre_window_match_reads_spaces() {
    let data = [0x02, 0x01, 0x00, 0xAA, 0x0A, 0, 0, 0];
    let px = crate::decode_mip(0x4444, &data, 2, 1).unwrap();
    assert_eq!(px, [0, 34, 0, 34, 170, 34, 0, 170]);
}

#[test]
fn lzss_zero_distance_is_an_error() {
    let data = [0x00, 0x00, 0x00, 0, 0, 0, 0];
    let e = crate::decode_mip(0x4444, &data, 3, 1).unwrap_err();
    assert!(e.to_string().contains("zero distance"), "{e}");
}

// The second byte of a match sits in the 4-byte checksum: that is not data. Without the bound the
// stream below decodes (b1 = 0x60 is also the checksum's low byte, so everything else is consistent).
#[test]
fn lzss_match_cannot_read_its_second_byte_from_the_checksum() {
    let data = [0x00, 0x01, 0x60, 0, 0, 0];
    let e = crate::decode_mip(0x4444, &data, 3, 1).unwrap_err();
    assert!(e.to_string().contains("truncated match"), "{e}");
}

// --- PAA length field -------------------------------------------------------

#[test]
fn paa_mip_data_of_65536_bytes_or_more_round_trips() {
    // Bit 16 and above of the 24-bit length live in the third length byte, which the reader shifts
    // by 16. Every 1024-wide DXT mip is over 64 KB, so this is the common case in real files.
    for n in [0x1_0000usize, 70_000, 0x12_3456] {
        let big: Vec<u8> = (0..n).map(|i| (i % 251) as u8).collect();
        let mips = vec![
            Mip {
                w: 4,
                h: 4,
                lzo: false,
                data: big.clone(),
            },
            Mip {
                w: 4,
                h: 4,
                lzo: true,
                data: vec![5; 9],
            },
        ];
        let q = paa::read(
            &paa::write(&Paa {
                ty: paa::TYPE_DXT1,
                tags: vec![],
                mips,
            })
            .unwrap(),
        )
        .unwrap();
        assert_eq!(q.mips.len(), 2, "{n}");
        assert!(q.mips[0].data == big, "{n}");
        assert_eq!(
            (q.mips[1].lzo, q.mips[1].data.as_slice()),
            (true, &[5u8; 9][..])
        );
    }
}

// --- reader on files with no mips ---------------------------------------------------------------

// ImageToPAA writes a 1x1 or 2x2 image as a header with no mip entries and no end marker (a
// 112 to 128 byte file). The reader needs the end marker and calls it truncated. Pinned as the
// documented behaviour (a known limit): the dev CLI is the only reader.
#[test]
fn a_header_with_no_mips_and_no_end_marker_is_rejected_as_truncated() {
    let full = paa::write(&Paa {
        ty: paa::TYPE_DXT1,
        tags: vec![],
        mips: vec![],
    })
    .unwrap();
    assert_eq!(
        paa::read(&full).unwrap().mips.len(),
        0,
        "with the end marker it reads"
    );
    let e = paa::read(&full[..full.len() - 6])
        .err()
        .expect("no end marker");
    assert!(e.to_string().contains("truncated"), "{e}");
}

// --- every type: round trip and header mutation -----------------------------------------------------------

/// (rule suffix, expected PAA type word) for the five types the crate writes.
const TYPES: [(&str, u16); 5] = [
    ("co", 0xFF01),
    ("ca", 0xFF05),
    ("1555", 0x1555),
    ("4444", 0x4444),
    ("88", 0x8080),
];

#[test]
fn every_type_round_trips_to_the_quantised_pixels() {
    use crate::rules::Fmt;
    for (suffix, ty) in TYPES {
        let src = solid(16, 16, 255);
        let b = encode::encode(
            Image {
                w: 16,
                h: 16,
                rgba: src.rgba.clone(),
            },
            rule(suffix),
        )
        .unwrap();
        let p = paa::read(&b).unwrap();
        assert_eq!(p.ty, ty, "_{suffix}");
        let m = &p.mips[0];
        let got = crate::decode_mip(p.ty, &m.data, m.w as usize, m.h as usize).unwrap();
        assert_eq!(got.len(), 16 * 16 * 4, "_{suffix}");
        let want = match ty {
            0x1555 => Some(Fmt::Argb1555),
            0x4444 => Some(Fmt::Argb4444),
            0x8080 => Some(Fmt::Ai88),
            _ => None,
        };
        match want {
            // Pixel formats are exact: the stored mip decodes to the packed source.
            Some(f) => assert_eq!(
                got,
                crate::argb::unpack(&crate::argb::pack(&src.rgba, f), f),
                "_{suffix}"
            ),
            // DXT is lossy; the pattern is noisy, so the bound is loose and only catches a wrong layout.
            None => {
                let err: u32 = got
                    .iter()
                    .zip(&src.rgba)
                    .map(|(&a, &b)| a.abs_diff(b) as u32)
                    .sum();
                assert!(
                    err / got.len() as u32 <= 20,
                    "_{suffix}: mean error {}",
                    err / got.len() as u32
                );
            }
        }
    }
}

// The reader is fed every one-byte corruption of a header and every truncation of each seed file;
// it may refuse but never panic or hang. Pixel decoding is attempted only on small mips so that a
// corrupted size cannot ask for gigabytes (that hazard is the huge-declared-size test above).
#[test]
fn header_corruption_and_truncation_never_panic() {
    for (suffix, _) in TYPES {
        let seed = encode::encode(solid(16, 16, 200), rule(suffix)).unwrap();
        for i in 0..seed.len().min(220) {
            for v in [0x00, 0x01, 0x7F, 0x80, 0xFF] {
                let mut b = seed.clone();
                if b[i] == v {
                    continue;
                }
                b[i] = v;
                if let Ok(p) = paa::read(&b) {
                    for m in p
                        .mips
                        .iter()
                        .filter(|m| (m.w as usize) * (m.h as usize) <= 1 << 16)
                    {
                        let _ = crate::decode_mip(p.ty, &m.data, m.w as usize, m.h as usize);
                    }
                }
            }
        }
        for n in 0..seed.len() {
            let _ = paa::read(&seed[..n]);
        }
    }
}

// --- DXT block invariants and DXT5 colour order ---------------------------------------

// The colour endpoints of every block the encoder writes are ordered for the mode it means: c0 > c1
// (four colours) unless the block is index 0 throughout, and for punch-through DXT1 a block with
// any transparent texel has c0 <= c1 (three colours, index 3 transparent). A block that broke this
// would decode in the other mode on every GPU.
#[test]
fn encoded_blocks_have_endpoints_ordered_for_their_mode() {
    use crate::dxt::{encode_image, Format};
    let mut s = 99u32;
    let mut rnd = move || {
        s = s.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        (s >> 24) as u8
    };
    for n in 0..3000 {
        let mut px = [[0u8; 4]; 16];
        let (base, spread) = [(rnd(), 0u8), (rnd(), 8), (rnd(), 64), (rnd(), 255)][n % 4];
        for p in px.iter_mut() {
            let j = |r: u8| base.wrapping_add((r as u32 * spread as u32 / 255) as u8);
            *p = [
                j(rnd()),
                j(rnd()),
                j(rnd()),
                [255, 255, 0, rnd()][n / 4 % 4],
            ];
        }
        for fmt in [
            Format::Dxt1 { punch: false },
            Format::Dxt1 { punch: true },
            Format::Dxt5,
        ] {
            // One 4x4 image is one block; its bytes are the block.
            let rgba: Vec<u8> = px.iter().flatten().copied().collect();
            let out = encode_image(&rgba, 4, 4, fmt);
            assert_eq!(out.len(), if fmt == Format::Dxt5 { 16 } else { 8 });
            let c = &out[out.len() - 8..];
            let (c0, c1) = (
                u16::from_le_bytes([c[0], c[1]]),
                u16::from_le_bytes([c[2], c[3]]),
            );
            let idx = u32::from_le_bytes([c[4], c[5], c[6], c[7]]);
            let holes = px.iter().any(|p| p[3] < 128);
            if matches!(fmt, Format::Dxt1 { punch: true }) && holes {
                assert!(c0 <= c1, "punch block with holes: {c0:04x} {c1:04x}");
                for (i, p) in px.iter().enumerate() {
                    assert_eq!(
                        p[3] < 128,
                        (idx >> (2 * i)) & 3 == 3 && c0 <= c1,
                        "texel {i} of block {n}"
                    );
                }
            } else {
                assert!(
                    c0 > c1 || idx == 0,
                    "{fmt:?} block {n}: c0 {c0:04x} c1 {c1:04x} idx {idx:08x}"
                );
            }
        }
    }
}

// A DXT5 block whose c0 <= c1 is decoded with the 3-colour palette (midpoint at index 2, black at
// index 3), the same as ImageToPAA's own reader does. The encoder never writes one (above). What a
// real BC3 GPU does with it is unproven, so this pins our decoder, not the hardware.
#[test]
fn dxt5_block_with_c0_below_c1_decodes_with_the_3_colour_palette() {
    let mut blk = vec![255u8, 255, 0, 0, 0, 0, 0, 0]; // alpha: a0 = a1 = 255, every index 0
    blk.extend_from_slice(&0x0000u16.to_le_bytes()); // c0 black
    blk.extend_from_slice(&0xF800u16.to_le_bytes()); // c1 red
    blk.extend_from_slice(&(2u32 | 3 << 2 | 1 << 6).to_le_bytes()); // texels 0..3: indices 2, 3, 0, 1
    let out = crate::decode_image(&blk, 4, 4, true).unwrap();
    assert_eq!(
        &out[..16],
        &[127, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 0, 0, 255]
    );
}

// --- big flat image -----------------------------------------------------------------------------------

// A flat black `_co` at the largest size: AVGC is 1,1,1 with alpha 255 (ImageToPAA gives 1 for
// opaque black in the linear classes, 0 in the data classes; seen on a 512x512 `_co` and on the
// alpha sweep), every mip down to 4x4 is written, and the whole file is tiny (LZO packs a flat DXT1
// mip to a few hundred bytes).
#[test]
fn flat_black_co_at_4096_has_avgc_one_and_a_small_file() {
    let img = Image {
        w: 4096,
        h: 4096,
        rgba: [0, 0, 0, 255].repeat(4096 * 4096),
    };
    let b = encode::encode(img, rule("co")).unwrap();
    let p = paa::read(&b).unwrap();
    assert_eq!(p.mips.len(), 11);
    assert_eq!((p.mips[0].w, p.mips[10].w), (4096, 4));
    assert_eq!(p.tags[0].name, "AVGC");
    assert_eq!(p.tags[0].data, [1, 1, 1, 255]);
    assert!(b.len() < 100_000, "{} bytes", b.len());
}
