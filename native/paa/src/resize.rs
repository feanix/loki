//! Output size (0.3.1, decision 51): fit a loaded image to a `w` x `h` output, with a fit mode, a pad
//! colour and an alignment. The one owner of this geometry and resampling: `encode_file` uses it, and
//! `fit_png` (the `_paa` module) writes the same pixels as an 8-bit PNG for an ImageToPAA hand-over.
//!
//! * `Stretch`: scale to exactly `w` x `h`.
//! * `Contain`: scale (same factor on both sides) to the largest size inside `w` x `h`, then place it on a
//!   `w` x `h` canvas filled with the pad colour.
//! * `Pad`: no scaling; place the image on the canvas. An image larger than the canvas is an error.
//!
//! Scaling. A scale that is an exact power-of-two halving (1024x512 to 256x128) is the rule's own mip step
//! (`mipfilter::next_mip`, applied until the size is reached), so a size equal to a mip level gives the mip
//! the normal path would store. Every other scale is Lanczos-3 on premultiplied alpha (so a transparent
//! edge leaves no dark or colour halo), in linear light for the classes that average mips in linear light
//! (`Rule::linear_mips`); normal-map classes are renormalised to unit length afterwards. The alpha is only
//! premultiplied when the rule stores the source alpha as alpha (otherwise it is a plain channel).

use crate::error::Error;
use crate::img::{Image, MAX_DIM};
use crate::mipfilter;
use crate::rules::{Rule, Src};

/// How the image is made to fit the output size.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Fit {
    /// Scale to exactly the output size (the shape changes).
    Stretch,
    /// Scale to fit inside the output size keeping the shape; fill the rest.
    Contain,
    /// No scaling; place on the canvas; fill the rest. A larger image is refused.
    Pad,
}

impl Fit {
    /// The spelling used on the command line.
    pub fn name(self) -> &'static str {
        match self {
            Fit::Stretch => "stretch",
            Fit::Contain => "contain",
            Fit::Pad => "pad",
        }
    }

    /// Parse a command line spelling (lower case).
    pub fn parse(s: &str) -> Option<Fit> {
        [Fit::Stretch, Fit::Contain, Fit::Pad]
            .into_iter()
            .find(|f| f.name() == s)
    }
}

/// Where the image sits on the canvas when it does not fill it.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Align {
    /// Top left corner.
    TopLeft,
    /// Top edge, centred.
    Top,
    /// Top right corner.
    TopRight,
    /// Left edge, centred.
    Left,
    /// Centred (the default).
    Centre,
    /// Right edge, centred.
    Right,
    /// Bottom left corner.
    BottomLeft,
    /// Bottom edge, centred.
    Bottom,
    /// Bottom right corner.
    BottomRight,
}

impl Align {
    /// The spelling used on the command line (`centre`, not `center`).
    pub fn name(self) -> &'static str {
        match self {
            Align::TopLeft => "top-left",
            Align::Top => "top",
            Align::TopRight => "top-right",
            Align::Left => "left",
            Align::Centre => "centre",
            Align::Right => "right",
            Align::BottomLeft => "bottom-left",
            Align::Bottom => "bottom",
            Align::BottomRight => "bottom-right",
        }
    }

    /// Parse a command line spelling (lower case); `center` is accepted for `centre`.
    pub fn parse(s: &str) -> Option<Align> {
        const ALL: [Align; 9] = [
            Align::TopLeft,
            Align::Top,
            Align::TopRight,
            Align::Left,
            Align::Centre,
            Align::Right,
            Align::BottomLeft,
            Align::Bottom,
            Align::BottomRight,
        ];
        let s = if s == "center" { "centre" } else { s };
        ALL.into_iter().find(|a| a.name() == s)
    }

    /// Offset of an `inner` span inside an `outer` one on each axis: (x, y) for a `w` x `h` image on a
    /// `cw` x `ch` canvas. A half is rounded down.
    fn offset(self, (w, h): (usize, usize), (cw, ch): (usize, usize)) -> (usize, usize) {
        let (dx, dy) = (cw - w, ch - h);
        let x = match self {
            Align::TopLeft | Align::Left | Align::BottomLeft => 0,
            Align::Top | Align::Centre | Align::Bottom => dx / 2,
            Align::TopRight | Align::Right | Align::BottomRight => dx,
        };
        let y = match self {
            Align::TopLeft | Align::Top | Align::TopRight => 0,
            Align::Left | Align::Centre | Align::Right => dy / 2,
            Align::BottomLeft | Align::Bottom | Align::BottomRight => dy,
        };
        (x, y)
    }
}

/// The output size and how to reach it.
#[derive(Clone, Copy, Debug)]
pub struct SizeSpec {
    /// Output width (a power of two, 1..=`MAX_DIM`).
    pub w: usize,
    /// Output height (a power of two, 1..=`MAX_DIM`).
    pub h: usize,
    /// Fit mode.
    pub fit: Fit,
    /// Pad colour RGBA in source space (before any swizzle); `None` = the rule's default (`Rule::pad_colour`).
    pub colour: Option<[u8; 4]>,
    /// Alignment of the image on the canvas.
    pub align: Align,
}

/// The size of `w` x `h` scaled to fit inside `cw` x `ch` with the same factor on both sides: one side
/// equals the canvas, the other is rounded to the nearest pixel (at least 1).
fn contain_size((w, h): (usize, usize), (cw, ch): (usize, usize)) -> (usize, usize) {
    // side * num / den rounded to nearest, at least 1.
    let scaled = |side: usize, num: usize, den: usize| ((2 * side * num + den) / (2 * den)).max(1);
    // Compare cw/w with ch/h without division: cw*h <= ch*w means the width is the limit.
    if cw * h <= ch * w {
        (cw, scaled(h, cw, w).min(ch))
    } else {
        (scaled(w, ch, h).min(cw), ch)
    }
}

/// Lanczos-3 kernel.
fn lanczos(x: f64) -> f64 {
    let x = x.abs();
    if x >= 3.0 {
        0.0
    } else if x < 1e-9 {
        1.0
    } else {
        let px = std::f64::consts::PI * x;
        3.0 * px.sin() * (px / 3.0).sin() / (px * px)
    }
}

/// For each of `dst` output positions along one axis of `src` samples: the first source index and the
/// normalised weights (only in-range samples take part, so an edge needs no extra rule). A downscale
/// widens the kernel by the scale factor so it low-pass filters.
fn axis_weights(src: usize, dst: usize) -> Vec<(usize, Vec<f32>)> {
    let scale = src as f64 / dst as f64;
    let width = scale.max(1.0);
    (0..dst)
        .map(|i| {
            let centre = (i as f64 + 0.5) * scale;
            let lo = ((centre - 3.0 * width).floor().max(0.0)) as usize;
            let hi = (((centre + 3.0 * width).ceil()) as usize).min(src);
            let mut w: Vec<f64> = (lo..hi)
                .map(|k| lanczos((k as f64 + 0.5 - centre) / width))
                .collect();
            let sum: f64 = w.iter().sum();
            if sum.abs() < 1e-12 {
                // Cannot happen for a positive-width kernel over at least one sample; stay safe.
                let nearest = (centre as usize).min(src - 1);
                return (nearest, vec![1.0]);
            }
            w.iter_mut().for_each(|v| *v /= sum);
            (lo, w.into_iter().map(|v| v as f32).collect())
        })
        .collect()
}

/// sRGB byte to the working value (linear light when `linear`, else the byte over 255).
fn work_lut(linear: bool) -> [f32; 256] {
    std::array::from_fn(|v| {
        if linear {
            mipfilter::to_linear(v as u8) as f32
        } else {
            v as f32 / 255.0
        }
    })
}

/// Resample along x (`horizontal`) or y: `buf` holds `w * h` pixels of 4 f32.
fn pass(buf: &[f32], (w, h): (usize, usize), horizontal: bool, to: usize) -> Vec<f32> {
    if horizontal {
        let wts = axis_weights(w, to);
        let mut out = vec![0f32; to * h * 4];
        for y in 0..h {
            let row = &buf[y * w * 4..(y + 1) * w * 4];
            for (x, (lo, ws)) in wts.iter().enumerate() {
                let mut acc = [0f32; 4];
                for (k, wt) in ws.iter().enumerate() {
                    let p = &row[(lo + k) * 4..(lo + k) * 4 + 4];
                    for c in 0..4 {
                        acc[c] += p[c] * wt;
                    }
                }
                out[(y * to + x) * 4..][..4].copy_from_slice(&acc);
            }
        }
        out
    } else {
        let wts = axis_weights(h, to);
        let mut out = vec![0f32; w * to * 4];
        for (y, (lo, ws)) in wts.iter().enumerate() {
            let orow = &mut out[y * w * 4..(y + 1) * w * 4];
            for (k, wt) in ws.iter().enumerate() {
                let row = &buf[(lo + k) * w * 4..(lo + k + 1) * w * 4];
                for (o, v) in orow.iter_mut().zip(row) {
                    *o += v * wt;
                }
            }
        }
        out
    }
}

/// Scale by Lanczos-3 on premultiplied alpha to `nw` x `nh`. `premult`: weight the colour by alpha;
/// `linear`: work in linear light.
fn lanczos_scale(img: &Image, (nw, nh): (usize, usize), premult: bool, linear: bool) -> Image {
    let lut = work_lut(linear);
    let mut buf = Vec::with_capacity(img.w * img.h * 4);
    for p in img.rgba.chunks_exact(4) {
        let a = p[3] as f32 / 255.0;
        let m = if premult { a } else { 1.0 };
        buf.extend_from_slice(&[
            lut[p[0] as usize] * m,
            lut[p[1] as usize] * m,
            lut[p[2] as usize] * m,
            a,
        ]);
    }
    // Shrink the larger axis first so the intermediate buffer stays the smaller one.
    let size = (img.w, img.h);
    let (buf, size) = if nw * img.h <= img.w * nh {
        let b = pass(&buf, size, true, nw);
        (pass(&b, (nw, img.h), false, nh), (nw, nh))
    } else {
        let b = pass(&buf, size, false, nh);
        (pass(&b, (img.w, nh), true, nw), (nw, nh))
    };
    let mut rgba = Vec::with_capacity(size.0 * size.1 * 4);
    for p in buf.chunks_exact(4) {
        // Lanczos overshoots: keep alpha in 0..1 and each premultiplied colour in 0..alpha, so the
        // colour over alpha stays a valid colour (a flat colour stays exactly that colour).
        let a = p[3].clamp(0.0, 1.0);
        let div = if premult { a } else { 1.0 };
        for &pc in &p[..3] {
            let v = if div > 0.0 {
                (pc / div).clamp(0.0, 1.0)
            } else {
                0.0
            };
            rgba.push(if linear {
                mipfilter::from_linear(v as f64)
            } else {
                (v * 255.0).round() as u8
            });
        }
        rgba.push((a * 255.0).round() as u8);
    }
    Image {
        w: size.0,
        h: size.1,
        rgba,
    }
}

/// True when the stored alpha is the source alpha (so transparent texels carry no colour of their own).
fn alpha_is_transparency(rule: &Rule) -> bool {
    matches!(rule.swizzle[3], None | Some(Src::A))
}

/// `img` at `nw` x `nh`: untouched when the size is the same, the rule's mip step when it is an exact
/// power-of-two halving of both sides, else Lanczos on premultiplied alpha (normal maps renormalised).
fn scale(img: Image, (nw, nh): (usize, usize), rule: &Rule) -> Image {
    if (img.w, img.h) == (nw, nh) {
        return img;
    }
    let halvings = (1..=12).find(|k| img.w == nw << k && img.h == nh << k);
    if let Some(k) = halvings {
        return (0..k).fold(img, |m, _| {
            mipfilter::next_mip(&m, rule.mip_filter, rule.linear_mips)
        });
    }
    let mut out = lanczos_scale(
        &img,
        (nw, nh),
        alpha_is_transparency(rule),
        rule.linear_mips,
    );
    if mipfilter::is_normal_filter(rule.mip_filter) {
        mipfilter::renormalise(&mut out.rgba);
    }
    out
}

/// Put `img` on a `cw` x `ch` canvas of `fill`, at the alignment's corner or edge.
fn place(img: Image, (cw, ch): (usize, usize), fill: [u8; 4], align: Align) -> Image {
    if (img.w, img.h) == (cw, ch) {
        return img;
    }
    let mut rgba: Vec<u8> = fill.repeat(cw * ch);
    let (x0, y0) = align.offset((img.w, img.h), (cw, ch));
    for y in 0..img.h {
        let dst = ((y0 + y) * cw + x0) * 4;
        rgba[dst..dst + img.w * 4].copy_from_slice(&img.rgba[y * img.w * 4..(y + 1) * img.w * 4]);
    }
    Image { w: cw, h: ch, rgba }
}

/// The image at the output size of `spec`, by `rule` (its mip filter, linear flag and default pad colour).
/// Err(`Size`) for a bad output size (not a power of two, or over `MAX_DIM`), a bad buffer, or `Pad` with an
/// image larger than the canvas.
pub fn fit_image(img: Image, spec: &SizeSpec, rule: &Rule) -> Result<Image, Error> {
    img.check()?;
    let (cw, ch) = (spec.w, spec.h);
    if !cw.is_power_of_two() || !ch.is_power_of_two() || cw > MAX_DIM || ch > MAX_DIM {
        return Err(Error::Size(format!(
            "output size {cw}x{ch}: both sides must be powers of two from 1 to {MAX_DIM}"
        )));
    }
    let fill = spec.colour.unwrap_or_else(|| rule.pad_colour());
    let placed = match spec.fit {
        Fit::Stretch => scale(img, (cw, ch), rule),
        Fit::Contain => {
            let size = contain_size((img.w, img.h), (cw, ch));
            scale(img, size, rule)
        }
        Fit::Pad => {
            if img.w > cw || img.h > ch {
                return Err(Error::Size(format!(
                    "size {}x{} is larger than the output size {cw}x{ch}: pad does not resize (use contain or stretch, or a larger output size)",
                    img.w, img.h
                )));
            }
            img
        }
    };
    Ok(place(placed, (cw, ch), fill, spec.align))
}
