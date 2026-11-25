//! Texture encoding policy: choose the storage format by rule, apply the channel swizzle and mip
//! filter, write the tags, and add the LZO layer.
//!
//! The per-suffix facts live in `rules.rs`; this file applies them. The rules were reverse
//! engineered by running ImageToPAA (Bohemia Interactive's reference converter) on probe images:
//! alpha handling, FLAG values, SWIZ codes, uncompressed formats, when LZO is used. Each rule
//! below is marked "observed" (seen in ImageToPAA output) or "inferred" (a guess that fits the
//! probes; ImageToPAA was not seen to do it).
//!
//! Tags are the small named records before the mip data: AVGC (average colour), MAXC (max colour,
//! always white), FLAG (alpha kind: 1 soft, 2 on/off), SWIZ (channel swizzle codes) and OFFS (mip
//! offsets, written by `paa::write`). `enableDXT`, `dynRange` and `autoreduce` are keys of the
//! converter's per-suffix config; see `rules.rs`.

use crate::argb;
use crate::avgc;
use crate::cutout;
use crate::dxt::{self, Format};
use crate::error::Error;
use crate::img::{self, Image, MAX_DIM};
use crate::mipfilter;
use crate::paa::{self, Mip, Paa, Tag};
use crate::resize::{self, SizeSpec};
use crate::rules::{self, Fmt, Rule, Src};
use std::borrow::Cow;
use std::path::Path;

/// The encoding rule for a file name stem (no extension): the class of its suffix, else the
/// unsuffixed default (`rules::PLAIN`; an unknown suffix gets it too, see `rules::for_suffix`).
/// Case-insensitive. Never fails: whether a rule can be encoded or routed here is
/// `Rule::unreplicable` / `Rule::routable`.
pub fn classify(stem: &str) -> &'static Rule {
    stem.rsplit_once('_')
        .map_or(&rules::PLAIN, |(_, suffix)| rules::for_suffix(suffix))
}

/// True for the file stems the caller may send to this encoder: the stem's rule is routable
/// (verified and implemented). No suffix and an unknown suffix both get `PLAIN`, which is.
pub(crate) fn stem_supported(stem: &str) -> bool {
    classify(stem).routable()
}

/// Alpha below this counts as "real" transparency for the FLAG tag. Observed: ImageToPAA
/// flagged a single pixel of alpha 235 and did not flag 240.
const FLAG_ALPHA_BELOW: u8 = 240;

/// FLAG tag value for a DXT5 or ARGB4444/AI88 (soft alpha) texture with real transparency.
/// Observed.
const FLAG_ALPHA: u32 = 1;
/// FLAG tag value for DXT1, ARGB1555, and binary (0/255) alpha in ARGB4444/AI88. Observed.
const FLAG_PUNCH: u32 = 2;

/// How the texels are stored.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum Layout {
    /// DXT blocks.
    Block(Format),
    /// ARGB1555, ARGB4444 or AI88, LZSS packed.
    Pixels(Fmt),
}

/// The chosen storage layout and FLAG tag for one image.
pub(crate) struct Plan {
    /// How the texels are stored.
    pub layout: Layout,
    /// FLAG tag value, if any.
    pub flag: Option<u32>,
}

impl Plan {
    /// The PAA type word that goes with the layout.
    fn ty(&self) -> u16 {
        match self.layout {
            Layout::Block(Format::Dxt1 { .. }) => paa::TYPE_DXT1,
            Layout::Block(Format::Dxt5) => paa::TYPE_DXT5,
            Layout::Pixels(f) => argb::type_word(f),
        }
    }
}

/// True when the stored alpha is the source alpha (no swizzle on A, or A from A).
fn alpha_passes(rule: &Rule) -> bool {
    matches!(rule.swizzle[3], None | Some(Src::A))
}

/// Smallest side a DXT block layout can hold (one 4x4 block).
const DXT_MIN_SIDE: usize = 4;

/// True when a side is under `DXT_MIN_SIDE`, so the image cannot be stored as DXT.
fn is_tiny(img: &Image) -> bool {
    img.w < DXT_MIN_SIDE || img.h < DXT_MIN_SIDE
}

/// Pick layout and FLAG from the rule and the image's alpha. Observed on every rule class
/// (synthetic opaque, soft-alpha and 0/255-alpha images).
pub(crate) fn plan(rule: &Rule, img: &Image) -> Plan {
    let (mut min_a, mut binary) = (255u8, true);
    for p in img.rgba.chunks_exact(4) {
        min_a = min_a.min(p[3]);
        binary &= p[3] == 0 || p[3] == 255;
    }
    let flag_alpha = min_a < FLAG_ALPHA_BELOW;
    let tiny = is_tiny(img);
    // DXT1 1-bit alpha only where the stored alpha is the source alpha.
    let punch = alpha_passes(rule);
    let dxt1 = |punch: bool, flag: Option<u32>| Plan {
        layout: Layout::Block(Format::Dxt1 { punch }),
        flag,
    };
    let dxt5 = |flag: Option<u32>| Plan {
        layout: Layout::Block(Format::Dxt5),
        flag,
    };
    let pixels = |f: Fmt, flag: Option<u32>| Plan {
        layout: Layout::Pixels(f),
        flag,
    };
    // 4444 and AI88: FLAG 2 for 0/255 alpha, 1 for soft alpha.
    let soft_flag = || flag_alpha.then_some(if binary { FLAG_PUNCH } else { FLAG_ALPHA });
    match rule.format {
        // Opaque (min alpha 255, or only slightly below): DXT1 with no FLAG. Any real
        // transparency: FLAG 2, with 1-bit alpha where the stored alpha is the source alpha.
        // FLAG 2 was observed on soft and on 0/255 alpha for every DXT1 class except `_co`,
        // whose behaviour is inferred from the unsuffixed 0/255 case.
        Some(Fmt::Dxt1) => {
            if flag_alpha {
                dxt1(punch, Some(FLAG_PUNCH))
            } else {
                dxt1(false, None)
            }
        }
        // autoreduce=1 (config key): an opaque image is stored as DXT1 even when the rule says
        // DXT5. Observed on `_pr`.
        Some(Fmt::Dxt5) => {
            if rule.autoreduce == Some(1) && min_a == 255 {
                dxt1(false, None)
            } else {
                dxt5(flag_alpha.then_some(FLAG_ALPHA))
            }
        }
        Some(Fmt::Argb1555) => pixels(Fmt::Argb1555, flag_alpha.then_some(FLAG_PUNCH)),
        Some(f @ (Fmt::Argb4444 | Fmt::Ai88)) => pixels(f, soft_flag()),
        // No format and enableDXT=0: ARGB1555 for opaque or 0/255 alpha, else ARGB4444. Observed.
        // Also for a side under 4 px: ImageToPAA cannot make DXT blocks there and falls back to the
        // same choice for every class that does not force a DXT format (observed on 40 classes,
        // sizes 1x1, 2x2, 1x256, 256x1, 4x1).
        None if rule.enable_dxt == Some(0) || tiny => {
            if binary {
                pixels(Fmt::Argb1555, flag_alpha.then_some(FLAG_PUNCH))
            } else {
                pixels(Fmt::Argb4444, soft_flag())
            }
        }
        // The TexConvert default (unsuffixed files, `_normalmap`, `_ns`, `_nof`).
        // Observed: all alpha 255 gives DXT1; only 0/255 gives DXT1 + FLAG 2; else DXT5.
        None => {
            if min_a == 255 {
                dxt1(false, None)
            } else if binary {
                dxt1(punch, Some(FLAG_PUNCH))
            } else {
                dxt5(flag_alpha.then_some(FLAG_ALPHA))
            }
        }
    }
}

/// sRGB (0..1) to linear light: the standard IEC 61966-2-1 curve, a linear toe up to 0.04045
/// and a 2.4 power above.
fn srgb_to_linear(v: f64) -> f64 {
    if v <= 0.04045 {
        v / 12.92
    } else {
        ((v + 0.055) / 1.055).powf(2.4)
    }
}

/// Inverse of `srgb_to_linear` (linear light to sRGB); the toe threshold is 0.0031308.
fn linear_to_srgb(v: f64) -> f64 {
    if v <= 0.0031308 {
        v * 12.92
    } else {
        1.055 * v.powf(1.0 / 2.4) - 0.055
    }
}

/// AVGC value. Observed (ImageToPAA output): with `dynRange` 1 (or unset) colour is the alpha
/// weighted mean in LINEAR light, of the stored colour `avgc::soft_colour(c, a)` of each texel
/// (task #31: the oracle's per-texel colour for soft alpha, exact against the oracle), converted
/// back to sRGB; with `dynRange` 0 it is the plain mean of the bytes (data textures), weighted by
/// alpha; alpha is always the plain mean. Taken from the source image, before any swizzle.
/// Inferred for `dynRange` 0: the alpha weight (the closest fit on alpha textures, within 3 levels).
fn average(img: &Image, linear: bool) -> [u8; 4] {
    // Lookup table: byte value to (linear or plain) 0..1, so the pixel loop does no powf().
    let lut: Vec<f64> = (0..256)
        .map(|v| {
            if linear {
                srgb_to_linear(v as f64 / 255.0)
            } else {
                v as f64 / 255.0
            }
        })
        .collect();
    let mut sum = [0f64; 3];
    let mut asum = 0u64;
    for p in img.rgba.chunks_exact(4) {
        let wgt = p[3] as f64;
        for c in 0..3 {
            // The linear classes average the oracle's stored colour; the data classes the byte itself.
            let v = if linear {
                avgc::soft_colour(p[c], p[3])
            } else {
                p[c]
            };
            sum[c] += lut[v as usize] * wgt;
        }
        asum += p[3] as u64;
    }
    let n = (img.w * img.h) as f64;
    // Fully transparent image: avoid dividing by zero; the colour is then 0.
    let cn = if asum == 0 { 1.0 } else { asum as f64 };
    // Weighted colour mean, converted back to sRGB if it was averaged in linear light, as a byte.
    let m = |c: usize| {
        ((if linear {
            linear_to_srgb(sum[c] / cn)
        } else {
            sum[c] / cn
        }) * 255.0)
            .round()
            .clamp(0.0, 255.0) as u8
    };
    let a = ((asum as f64) / n).round() as u8;
    // AVGC stores bytes in B, G, R, A order, so swap R and B here.
    [m(2), m(1), m(0), a]
}

/// Halve with the rule's own mip filter and linear flag (`mipfilter::next_mip`, the step that builds
/// mip 1) until the longest side is at most `max`, so the result is ImageToPAA's mip at that size.
/// Fails when a side is odd or under 8, or when the pixel buffer is not `w * h * 4` bytes.
pub fn limit_size(mut img: Image, max: usize, rule: &Rule) -> Result<Image, Error> {
    img.check()?;
    while img.w.max(img.h) > max {
        if !img.w.is_multiple_of(2) || !img.h.is_multiple_of(2) || img.w < 8 || img.h < 8 {
            return Err(Error::Size("cannot downscale to the max size".into()));
        }
        img = mipfilter::next_mip(&img, rule.mip_filter, rule.linear_mips);
    }
    Ok(img)
}

/// A mip is LZO compressed by ImageToPAA only if its width is at least this. Observed:
/// 128x512 stored raw, 256x32 flagged. DXT only; the pixel formats use LZSS and never set the bit.
const LZO_MIN_WIDTH: u32 = 256;

/// True when every texel of the image is the same RGBA value.
fn is_flat(img: &Image) -> bool {
    img.rgba.chunks_exact(4).all(|p| p == &img.rgba[..4])
}

/// The mip chain in source space (before fade and swizzle). DXT stops once either side is under 8,
/// so the last level has a short side of 4 (a 16x64 image ends at 4x16). The pixel formats go on
/// until either side is 1 (observed: a 16x64 image gives 16x64, 8x32, 4x16, 2x8, 1x4).
fn build_chain(img: Image, rule: &Rule, pixels: bool) -> Vec<Image> {
    let more = |m: &Image| {
        if pixels {
            m.w > 1 && m.h > 1
        } else {
            m.w >= 8 && m.h >= 8
        }
    };
    let mut chain = vec![img];
    while let Some(last) = chain.last().filter(|m| more(m)) {
        let next = mipfilter::next_mip(last, rule.mip_filter, rule.linear_mips);
        chain.push(next);
    }
    chain
}

/// Apply the rule's mip filter output step (renormalise normal maps, fade toward flat for some
/// data textures; see `mipfilter`) and then its channel swizzle, giving the texels that get
/// stored. Borrows the input when neither applies.
pub(crate) fn finish<'a>(m: &'a Image, rule: &Rule) -> Cow<'a, [u8]> {
    let mut data = Cow::Borrowed(m.rgba.as_slice());
    if mipfilter::shapes_output(rule.mip_filter) {
        mipfilter::output(data.to_mut(), rule.mip_filter, m.w, m.h);
    }
    if rule.has_swizzle() {
        // Stored channel order R, G, B, A; an absent key keeps the channel.
        let src = [
            rule.swizzle[0].unwrap_or(Src::R),
            rule.swizzle[1].unwrap_or(Src::G),
            rule.swizzle[2].unwrap_or(Src::B),
            rule.swizzle[3].unwrap_or(Src::A),
        ];
        let out: Vec<u8> = data
            .chunks_exact(4)
            .flat_map(|p| src.map(|s| s.pick(p)))
            .collect();
        data = Cow::Owned(out);
    }
    data
}

/// The SWIZ tag payload: the code of each stored channel in the order A, R, G, B.
fn swiz_payload(rule: &Rule) -> Vec<u8> {
    let code = |i: usize, id: Src| rule.swizzle[i].unwrap_or(id).code();
    vec![
        code(3, Src::A),
        code(0, Src::R),
        code(1, Src::G),
        code(2, Src::B),
    ]
}

/// Err(`Size`) for a side under 4 px when the rule forces a DXT format (`_co`, `_ca`, `_nohq`, ...).
/// ImageToPAA fails there too (and leaves a truncated file), so there is nothing to hand over.
/// Rules that do not force DXT store such an image in an ARGB format (see `plan`).
pub(crate) fn small_side_check(img: &Image, rule: &Rule) -> Result<(), Error> {
    if is_tiny(img) && matches!(rule.format, Some(Fmt::Dxt1 | Fmt::Dxt5)) {
        let (w, h) = (img.w, img.h);
        let suffix = rule.suffix();
        return Err(Error::Size(format!(
            "size {w}x{h}: DXT formats need both sides of at least {DXT_MIN_SIDE} px (suffix _{suffix} is DXT); use a larger image or a suffix that is not DXT, such as _raw"
        )));
    }
    Ok(())
}

/// Encode an image to PAA bytes by `rule`. The image must be a power of two on both sides,
/// 1..=`MAX_DIM` (under 4 only for rules that do not force DXT, see `small_side_check`), with
/// `rgba.len() == w * h * 4`; anything else is an `Error::Size`. A rule with an unreplicable mip
/// filter (`Rule::unreplicable`) is an `Error::Unsupported`.
pub fn encode(mut img: Image, rule: &Rule) -> Result<Vec<u8>, Error> {
    let (w, h) = (img.w, img.h);
    if !w.is_power_of_two() || !h.is_power_of_two() {
        return Err(Error::Size(format!(
            "size {w}x{h}: only power-of-two sizes are supported"
        )));
    }
    if w > MAX_DIM || h > MAX_DIM {
        return Err(Error::Size(format!(
            "size {w}x{h} exceeds {MAX_DIM}; halve it first (`limit_size`; `encode_file` does)"
        )));
    }
    img.check()?;
    // The only unreplicable rules use a noise mip filter, whose noise is not reproduced here.
    if let Some(why) = rule.unreplicable() {
        return Err(Error::Unsupported {
            reason: "noise mip filter",
            detail: format!("suffix _{} {why}", rule.suffix()),
        });
    }
    small_side_check(&img, rule)?;
    let plan = plan(rule, &img);
    let avg = average(&img, rule.dyn_range.unwrap_or(1) == 1);
    let pixels = matches!(plan.layout, Layout::Pixels(_));
    // autoreduce: an image of one colour in every channel is stored as a single 4x4 mip (DXT only;
    // the pixel formats never shrink this way). Observed on four rule classes (including `_mask`,
    // `_lco`, `_pr`) but not `_co`. The average above is taken first; it is the same either way.
    if rule.autoreduce == Some(1) && !pixels && is_flat(&img) {
        let px: Vec<u8> = img.rgba[..4].repeat(16);
        img = Image {
            w: 4,
            h: 4,
            rgba: px,
        };
    }
    let chain = build_chain(img, rule, pixels);

    // Encode each level. DXT levels become raw block bytes, then may be LZO packed. Pixel-format
    // levels are LZSS packed on the spot (never LZO) and skip to the next level.
    let mut mips = Vec::with_capacity(chain.len());
    for m in &chain {
        let mut texels = finish(m, rule);
        // A DXT1 1-bit alpha mip: the mask is cut from the soft alpha of this level (the chain keeps it
        // soft), with the coverage of the level kept, as ImageToPAA's dithered masks do (`cutout`).
        if matches!(plan.layout, Layout::Block(Format::Dxt1 { punch: true })) {
            cutout::apply(texels.to_mut(), m.w, m.h);
        }
        let (w, h) = (m.w as u32, m.h as u32);
        let raw = match plan.layout {
            Layout::Block(fmt) => dxt::encode_image(&texels, m.w, m.h, fmt),
            Layout::Pixels(f) => {
                // LZSS only; the LZO bit is never set for pixel formats.
                mips.push(Mip {
                    w,
                    h,
                    lzo: false,
                    data: argb::lzss_pack(&argb::pack(&texels, f)),
                });
                continue;
            }
        };
        // A mip is stored LZO packed only if its width is at least `LZO_MIN_WIDTH` and packing
        // makes it smaller; otherwise the raw bytes are kept, as ImageToPAA does.
        if w >= LZO_MIN_WIDTH {
            let packed =
                lzokay_native::compress(&raw).map_err(|e| Error::Lzo(format!("lzo: {e}")))?;
            if packed.len() < raw.len() {
                mips.push(Mip {
                    w,
                    h,
                    lzo: true,
                    data: packed,
                });
                continue;
            }
        }
        mips.push(Mip {
            w,
            h,
            lzo: false,
            data: raw,
        });
    }

    let mut tags = vec![
        Tag {
            name: "AVGC".into(),
            data: avg.to_vec(),
        },
        // Observed: MAXC is always ff ff ff ff.
        Tag {
            name: "MAXC".into(),
            data: vec![255; 4],
        },
    ];
    // Observed tag order: AVGC, MAXC, FLAG, SWIZ, then OFFS, which `paa::write` appends itself.
    if let Some(f) = plan.flag {
        tags.push(Tag {
            name: "FLAG".into(),
            data: f.to_le_bytes().to_vec(),
        });
    }
    if rule.has_swizzle() {
        tags.push(Tag {
            name: "SWIZ".into(),
            data: swiz_payload(rule),
        });
    }
    paa::write(&Paa {
        ty: plan.ty(),
        tags,
        mips,
    })
}

/// The rule for the PNG at `png`, from its file name (see `classify`).
fn file_rule(png: &Path) -> Result<&'static Rule, Error> {
    let stem = png
        .file_stem()
        .and_then(|s| s.to_str())
        .ok_or_else(|| Error::Usage("bad input name".into()))?;
    Ok(classify(stem))
}

/// The rule's default pad colour for the file named `png` (`Rule::pad_colour`).
pub fn default_pad_colour(png: &Path) -> Result<[u8; 4], Error> {
    Ok(file_rule(png)?.pad_colour())
}

/// Write the PNG at `png` at the output size of `size` as an 8-bit RGBA PNG at `out`: the same pixels
/// `encode_file_sized` encodes (the rule comes from the name of `png`), for an ImageToPAA hand-over.
pub fn fit_png(png: &Path, out: &Path, size: &SizeSpec) -> Result<(), Error> {
    let rule = file_rule(png)?;
    let image = resize::fit_image(img::load_png(png)?, size, rule)?;
    img::save_png(out, &image)
}

/// Encode the PNG at `png` to a .paa at `paa`, halving first while the longest side exceeds
/// `max_size` or `MAX_DIM`, whichever is smaller (a PNG over 4096 is halved to fit, as ImageToPAA does).
///
/// The rule comes from the PNG's file name (see `classify`). Single-threaded. The output is
/// written in one `fs::write`; callers that need an atomic replace pass a temp path.
pub fn encode_file(png: &Path, paa: &Path, max_size: Option<usize>) -> Result<(), Error> {
    encode_file_sized(png, paa, max_size, None)
}

/// `encode_file` with an optional output size (`resize::fit_image`: any source size, the output is exactly
/// the size of `size`). With a `size`, `max_size` is not used (the caller refuses both).
pub fn encode_file_sized(
    png: &Path,
    paa: &Path,
    max_size: Option<usize>,
    size: Option<&SizeSpec>,
) -> Result<(), Error> {
    let rule = file_rule(png)?;
    // Same check as in `encode`, done first so a PNG is not decoded for nothing.
    if let Some(why) = rule.unreplicable() {
        return Err(Error::Unsupported {
            reason: "noise mip filter",
            detail: format!("suffix _{} {why}", rule.suffix()),
        });
    }
    let mut image = img::load_png(png)?;
    if let Some(spec) = size {
        image = resize::fit_image(image, spec, rule)?;
    } else {
        let cap = max_size.map_or(MAX_DIM, |m| m.min(MAX_DIM));
        // An oversize side that is not a power of two cannot be halved to a valid size: say so, not "cannot downscale".
        if image.w.max(image.h) > cap && !(image.w.is_power_of_two() && image.h.is_power_of_two()) {
            return Err(Error::Size(format!(
                "size {}x{}: only power-of-two sizes are supported",
                image.w, image.h
            )));
        }
        image = limit_size(image, cap, rule)?;
    }
    // A DXT rule on a tiny image is refused as a size problem (ImageToPAA fails there too).
    small_side_check(&image, rule)?;
    let bytes = encode(image, rule)?;
    std::fs::write(paa, &bytes).map_err(|e| Error::io(paa, e))
}
