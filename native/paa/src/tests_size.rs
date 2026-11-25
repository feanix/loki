//! Tests for the output size (0.3.1, decision 51): geometry of each fit mode, every alignment, the
//! default pad colour per class, the resampling quality (no halo, unit-length normals) and the
//! equality of a size that is a mip level with the mip path. Inputs are generated, no fixtures.

use crate::encode;
use crate::error::Error;
use crate::img::{self, Image};
use crate::resize::{fit_image, Align, Fit, SizeSpec};
use crate::tests::rule;
use std::path::PathBuf;

fn spec(w: usize, h: usize, fit: Fit) -> SizeSpec {
    SizeSpec {
        w,
        h,
        fit,
        colour: None,
        align: Align::Centre,
    }
}

/// A w x h image: red channel = x, green = y, blue 7, alpha 255.
fn gradient(w: usize, h: usize) -> Image {
    let mut rgba = Vec::with_capacity(w * h * 4);
    for y in 0..h {
        for x in 0..w {
            rgba.extend_from_slice(&[
                (x * 255 / w.max(1)) as u8,
                (y * 255 / h.max(1)) as u8,
                7,
                255,
            ]);
        }
    }
    Image { w, h, rgba }
}

fn flat(w: usize, h: usize, px: [u8; 4]) -> Image {
    Image {
        w,
        h,
        rgba: px.repeat(w * h),
    }
}

fn at(i: &Image, x: usize, y: usize) -> [u8; 4] {
    i.rgba[(y * i.w + x) * 4..][..4].try_into().unwrap()
}

#[test]
fn each_fit_mode_on_300x200_and_a_square_canvas() {
    let co = rule("co");
    for (cw, ch) in [(512, 256), (256, 256)] {
        for fit in [Fit::Stretch, Fit::Contain] {
            let out = fit_image(flat(300, 200, [9, 8, 7, 255]), &spec(cw, ch, fit), co).unwrap();
            assert_eq!((out.w, out.h), (cw, ch), "{fit:?} {cw}x{ch}");
        }
        // Pad: 300 is wider than 256, so the square canvas flags it; the 512x256 canvas takes it.
        let r = fit_image(flat(300, 200, [9, 8, 7, 255]), &spec(cw, ch, Fit::Pad), co);
        if cw == 512 {
            assert_eq!(r.unwrap().w, 512);
        } else {
            assert!(
                matches!(r, Err(Error::Size(ref m)) if m.contains("larger than the output size 256x256"))
            );
        }
    }
}

#[test]
fn contain_keeps_the_shape_and_fills_the_rest() {
    // 300x200 into 512x256: scale 1.28, 384x256 centred, 64 px of fill each side.
    let mut s = spec(512, 256, Fit::Contain);
    s.colour = Some([1, 2, 3, 4]);
    let out = fit_image(flat(300, 200, [200, 100, 50, 255]), &s, rule("co")).unwrap();
    assert_eq!(at(&out, 0, 100), [1, 2, 3, 4]);
    assert_eq!(at(&out, 63, 100), [1, 2, 3, 4]);
    assert_eq!(at(&out, 64, 100), [200, 100, 50, 255]);
    assert_eq!(at(&out, 447, 100), [200, 100, 50, 255]);
    assert_eq!(at(&out, 448, 100), [1, 2, 3, 4]);
    // Into 256x256: 256x171 (rounded), 85 rows of fill split 42 above and 43 below.
    let out = fit_image(
        flat(300, 200, [200, 100, 50, 255]),
        &spec(256, 256, Fit::Contain),
        rule("co"),
    )
    .unwrap();
    assert_eq!(at(&out, 10, 41), [0, 0, 0, 255]);
    assert_eq!(at(&out, 10, 42), [200, 100, 50, 255]);
    assert_eq!(at(&out, 10, 212), [200, 100, 50, 255]);
    assert_eq!(at(&out, 10, 213), [0, 0, 0, 255]);
}

#[test]
fn pad_places_without_scaling_and_refuses_a_larger_image() {
    let img = gradient(16, 8);
    let want = at(&img, 3, 5);
    let out = fit_image(img, &spec(64, 32, Fit::Pad), rule("co")).unwrap();
    assert_eq!(at(&out, 24 + 3, 12 + 5), want); // centred: (64-16)/2, (32-8)/2
    for (w, h) in [(8, 32), (64, 4), (8, 4)] {
        assert!(
            matches!(
                fit_image(gradient(16, 8), &spec(w, h, Fit::Pad), rule("co")),
                Err(Error::Size(_))
            ),
            "{w}x{h}"
        );
    }
}

#[test]
fn every_alignment_puts_the_image_in_its_place() {
    // A 2x2 white image on an 8x8 black canvas; the top left texel of the image is found by scan.
    let cases = [
        (Align::TopLeft, (0, 0)),
        (Align::Top, (3, 0)),
        (Align::TopRight, (6, 0)),
        (Align::Left, (0, 3)),
        (Align::Centre, (3, 3)),
        (Align::Right, (6, 3)),
        (Align::BottomLeft, (0, 6)),
        (Align::Bottom, (3, 6)),
        (Align::BottomRight, (6, 6)),
    ];
    for (align, (x0, y0)) in cases {
        let s = SizeSpec {
            w: 8,
            h: 8,
            fit: Fit::Pad,
            colour: Some([0, 0, 0, 255]),
            align,
        };
        let out = fit_image(flat(2, 2, [255, 255, 255, 255]), &s, rule("co")).unwrap();
        for y in 0..8 {
            for x in 0..8 {
                let inside = (x0..x0 + 2).contains(&x) && (y0..y0 + 2).contains(&y);
                assert_eq!(
                    at(&out, x, y)[0],
                    if inside { 255 } else { 0 },
                    "{align:?} at {x},{y}"
                );
            }
        }
    }
}

#[test]
fn align_and_fit_spellings() {
    assert_eq!(Align::parse("center"), Some(Align::Centre));
    assert_eq!(Align::parse("centre"), Some(Align::Centre));
    assert_eq!(Align::parse("bottom-left"), Some(Align::BottomLeft));
    assert_eq!(Align::parse("middle"), None);
    assert_eq!(Fit::parse("pad"), Some(Fit::Pad));
    assert_eq!(Fit::parse("fill"), None);
}

#[test]
fn default_pad_colour_by_class() {
    let flat_normal = [128, 128, 255, 255];
    for s in [
        "nohq",
        "no",
        "novhq",
        "nofhq",
        "nshq",
        "non",
        "normalmap",
        "nopx",
        "ns",
        "nof",
        "noex",
    ] {
        assert_eq!(rule(s).pad_colour(), flat_normal, "_{s}");
    }
    for s in ["as", "ads", "adshq"] {
        assert_eq!(rule(s).pad_colour(), [255, 255, 255, 255], "_{s}");
    }
    for s in [
        "ca", "lca", "cat", "can", "8888", "mc", "pr", "gs", "raw", "dxt5", "4444",
    ] {
        assert_eq!(rule(s).pad_colour(), [0, 0, 0, 0], "_{s}");
    }
    for s in [
        "", "co", "lco", "mask", "sky", "sm", "smdi", "detail", "cdt", "dxt1", "draftlco",
    ] {
        assert_eq!(rule(s).pad_colour(), [0, 0, 0, 255], "_{s}");
    }
}

#[test]
fn the_class_default_fills_and_an_explicit_colour_wins() {
    let out = fit_image(
        flat(4, 4, [9, 9, 9, 255]),
        &spec(8, 8, Fit::Pad),
        rule("nohq"),
    )
    .unwrap();
    assert_eq!(at(&out, 0, 0), [128, 128, 255, 255]);
    let mut s = spec(8, 8, Fit::Pad);
    s.colour = Some([5, 6, 7, 8]);
    assert_eq!(
        at(
            &fit_image(flat(4, 4, [9, 9, 9, 255]), &s, rule("nohq")).unwrap(),
            0,
            0
        ),
        [5, 6, 7, 8]
    );
}

#[test]
fn bad_output_sizes_are_refused() {
    for (w, h) in [(0, 4), (6, 4), (4, 12), (8192, 4)] {
        assert!(
            matches!(
                fit_image(gradient(4, 4), &spec(w, h, Fit::Stretch), rule("co")),
                Err(Error::Size(_))
            ),
            "{w}x{h}"
        );
    }
    assert!(fit_image(gradient(4, 4), &spec(1, 1, Fit::Stretch), rule("co")).is_ok());
}

#[test]
fn a_flat_colour_stays_that_colour_when_scaled() {
    for suffix in ["co", "raw", "ca"] {
        let out = fit_image(
            flat(48, 48, [200, 100, 30, 255]),
            &spec(64, 32, Fit::Stretch),
            rule(suffix),
        )
        .unwrap();
        assert!(
            out.rgba.chunks_exact(4).all(|p| p == [200, 100, 30, 255]),
            "_{suffix}"
        );
    }
}

/// A red opaque square on transparent padding, scaled by an inexact factor: no texel with any alpha
/// may be anything but red (no dark halo from transparent black, no colour fringe).
#[test]
fn no_halo_at_a_transparent_edge() {
    for suffix in ["ca", "raw", "", "lca"] {
        let mut img = flat(64, 64, [0, 0, 0, 0]);
        for y in 16..48 {
            for x in 16..48 {
                img.rgba[(y * 64 + x) * 4..][..4].copy_from_slice(&[255, 0, 0, 255]);
            }
        }
        for (w, h) in [(32usize, 16usize), (128, 128), (256, 32)] {
            let out = fit_image(
                Image {
                    w: 64,
                    h: 64,
                    rgba: img.rgba.clone(),
                },
                &spec(w, h, Fit::Stretch),
                rule(suffix),
            )
            .unwrap();
            let mut seen = 0;
            for p in out.rgba.chunks_exact(4).filter(|p| p[3] > 0) {
                assert_eq!(&p[..3], &[255, 0, 0], "_{suffix} {w}x{h} alpha {}", p[3]);
                seen += 1;
            }
            assert!(seen > 0);
        }
    }
}

/// Every texel of a resampled normal map is a unit vector (within 1 percent), padding included.
#[test]
fn normals_are_unit_length_after_resampling() {
    let mut img = gradient(64, 64);
    for (i, p) in img.rgba.chunks_exact_mut(4).enumerate() {
        // A tilted field of normals, not unit length at the byte level, with a seam.
        let (x, y) = ((i % 64) as f64 / 64.0, (i / 64) as f64 / 64.0);
        let v = [
            (x * 6.0).sin() * 0.8,
            (y * 5.0).cos() * 0.6,
            0.5 + 0.5 * (x * y),
        ];
        let n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
        for c in 0..3 {
            p[c] = (((v[c] / n) * 0.5 + 0.5) * 255.0).round() as u8;
        }
        p[3] = 255;
    }
    for suffix in ["nohq", "no", "ns", "nof", "novhq"] {
        for (w, h, fit) in [
            (64usize, 16usize, Fit::Stretch),
            (128, 32, Fit::Contain),
            (256, 256, Fit::Contain),
        ] {
            let out = fit_image(
                Image {
                    w: 64,
                    h: 64,
                    rgba: img.rgba.clone(),
                },
                &spec(w, h, fit),
                rule(suffix),
            )
            .unwrap();
            for p in out.rgba.chunks_exact(4) {
                let v = [p[0], p[1], p[2]].map(|c| c as f64 / 255.0 * 2.0 - 1.0);
                let len = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
                assert!(
                    (len - 1.0).abs() < 0.01,
                    "_{suffix} {w}x{h} {fit:?}: length {len} for {p:?}"
                );
            }
        }
    }
}

fn temp_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("paa-proto-size-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// A size equal to a mip level is the mip the normal path stores: `encode_file_sized` equals encoding
/// `limit_size` of the same image, byte for byte, for a colour, a normal map and an alpha class.
#[test]
fn a_size_equal_to_a_mip_level_matches_the_mip_path() {
    let dir = temp_dir("mip");
    let src = gradient(256, 128);
    for suffix in ["co", "nohq", "ca", "raw"] {
        let png = dir.join(format!("t_{suffix}.png"));
        img::save_png(&png, &src).unwrap();
        for (cw, ch, max) in [(128, 64, 128), (64, 32, 64), (32, 16, 32)] {
            let out = dir.join("o.paa");
            for fit in [Fit::Stretch, Fit::Contain] {
                encode::encode_file_sized(&png, &out, None, Some(&spec(cw, ch, fit))).unwrap();
                let want = encode::encode(
                    encode::limit_size(
                        Image {
                            w: 256,
                            h: 128,
                            rgba: src.rgba.clone(),
                        },
                        max,
                        rule(suffix),
                    )
                    .unwrap(),
                    rule(suffix),
                )
                .unwrap();
                assert_eq!(
                    std::fs::read(&out).unwrap(),
                    want,
                    "_{suffix} {cw}x{ch} {fit:?}"
                );
            }
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
}

/// `fit_png` writes the pixels `encode_file_sized` encodes: a PNG that, encoded at its own size, gives
/// the same PAA.
#[test]
fn fit_png_gives_the_pixels_the_encoder_uses() {
    let dir = temp_dir("fitpng");
    let png = dir.join("w_co.png");
    img::save_png(&png, &gradient(300, 200)).unwrap();
    let s = SizeSpec {
        w: 512,
        h: 256,
        fit: Fit::Contain,
        colour: Some([10, 20, 30, 255]),
        align: Align::TopLeft,
    };
    let sized = dir.join("sized").join("w_co.png");
    std::fs::create_dir_all(sized.parent().unwrap()).unwrap();
    encode::fit_png(&png, &sized, &s).unwrap();
    let (a, b) = (dir.join("a.paa"), dir.join("b.paa"));
    encode::encode_file_sized(&png, &a, None, Some(&s)).unwrap();
    encode::encode_file(&sized, &b, None).unwrap();
    assert_eq!(std::fs::read(&a).unwrap(), std::fs::read(&b).unwrap());
    let _ = std::fs::remove_dir_all(&dir);
}
