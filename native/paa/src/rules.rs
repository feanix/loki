//! The rule table: one entry per TextureHints class of Bohemia's TexConvert.cfg.
//!
//! ImageToPAA picks the pixel format, channel swizzle and mip filter for a texture from its
//! file name suffix (`foo_nohq.png`). This table holds those choices so the encoder can make
//! the same ones without Bohemia's tools.
//!
//! Matches the Arma 3 Tools ImageToPAA `TexConvert.cfg`, version 2.9.2014, `convertVersion=6`.
//! A script in the repo checks the table against an installed copy of that file. This is our
//! own text: the facts of each class (suffix, format, swizzle, mip filter and the other keys),
//! written by hand. Bohemia's file is not copied into the repo or the exe.
//!
//! `Rule::routable` is the gate for routing: a suffix is sent to this encoder only when its
//! rule is verified (its output matched ImageToPAA's in side-by-side checks) and this crate
//! can reproduce its mip filter. Nothing else may set `verified`.
//!
//! Keys of the cfg, and what this crate does with each:
//! * acted on: `name` (suffix lookup), `format`, `enableDXT` (default format when `format` is
//!   unset), `channelSwizzleR/G/B/A`, `mipmapFilter`, `dynRange` (1 or unset: the average-colour
//!   (AVGC) tag in the PAA header is computed in linear light, 0: plain sRGB; see `linear_mips`),
//!   `autoreduce` (flat image: one 4x4 mip; DXT5 with an opaque image: DXT1).
//! * recorded, not acted on: `errorMetrics` (the DXT fit here always minimises RGB distance).
//! * any other key in a cfg is "unknown": the diff script lists it and fails (nothing is dropped
//!   silently); the 2.9.2014 file has none.

/// Block or pixel format named by the cfg `format` key. The cfg also names `Default`, `P8` and
/// `DXT2` to `DXT4`; no class uses them, so they are not modelled.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Fmt {
    /// `DXT1`.
    Dxt1,
    /// `DXT5`.
    Dxt5,
    /// `ARGB1555`: 1-bit alpha, 5 bits per colour channel, LZSS packed.
    Argb1555,
    /// `ARGB4444`: 4 bits per channel, LZSS packed.
    Argb4444,
    /// `AI88`: grey (BT.601 luma) plus alpha, 8 bits each, LZSS packed.
    Ai88,
}

impl Fmt {
    /// The spelling used in TexConvert.cfg.
    pub fn cfg(self) -> &'static str {
        match self {
            Fmt::Dxt1 => "DXT1",
            Fmt::Dxt5 => "DXT5",
            Fmt::Argb1555 => "ARGB1555",
            Fmt::Argb4444 => "ARGB4444",
            Fmt::Ai88 => "AI88",
        }
    }
}

/// Where a stored channel comes from: a `channelSwizzle*` value (`"R"`, `"1-A"`, `"1"`, `"0"`).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Src {
    /// Source alpha.
    A,
    /// Source red.
    R,
    /// Source green.
    G,
    /// Source blue.
    B,
    /// `1-A`.
    InvA,
    /// `1-R`.
    InvR,
    /// `1-G`.
    InvG,
    /// `1-B`.
    InvB,
    /// Constant `"1"` (255).
    One,
    /// Constant `"0"` (0).
    Zero,
}

impl Src {
    /// The code stored in the SWIZ tag. Observed in ImageToPAA output: A=0 R=1 G=2 B=3,
    /// inverted = +4, One=8, Zero=9. The four SWIZ bytes are in the order A, R, G, B of the
    /// stored channel (for `_nohq`, which stores 1-R, 1-A, G, B, that gives 05 04 02 03).
    pub fn code(self) -> u8 {
        match self {
            Src::A => 0,
            Src::R => 1,
            Src::G => 2,
            Src::B => 3,
            Src::InvA => 4,
            Src::InvR => 5,
            Src::InvG => 6,
            Src::InvB => 7,
            Src::One => 8,
            Src::Zero => 9,
        }
    }

    /// The spelling used in TexConvert.cfg.
    pub fn cfg(self) -> &'static str {
        match self {
            Src::A => "A",
            Src::R => "R",
            Src::G => "G",
            Src::B => "B",
            Src::InvA => "1-A",
            Src::InvR => "1-R",
            Src::InvG => "1-G",
            Src::InvB => "1-B",
            Src::One => "1",
            Src::Zero => "0",
        }
    }

    /// The stored value of this channel for the source texel `px` (RGBA).
    pub(crate) fn pick(self, px: &[u8]) -> u8 {
        match self {
            Src::R => px[0],
            Src::G => px[1],
            Src::B => px[2],
            Src::A => px[3],
            Src::InvR => 255 - px[0],
            Src::InvG => 255 - px[1],
            Src::InvB => 255 - px[2],
            Src::InvA => 255 - px[3],
            Src::One => 255,
            Src::Zero => 0,
        }
    }
}

/// The cfg `mipmapFilter` values the classes use. An unset filter is `Default` (plain box).
/// Variant names are the cfg spellings, so `{:?}` prints the TexConvert.cfg value.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Filter {
    /// Fade every channel toward 128 as the mip gets small (detail textures).
    FadeOut,
    /// Alpha replaced by noise (alpha test). Not replicated: ImageToPAA's noise is its own.
    AlphaNoise,
    /// Noise added to alpha. Not replicated.
    AddAlphaNoise,
    /// Box average of the normal vectors, renormalised; alpha box-averaged.
    NormalizeNormalMap,
    /// As `NormalizeNormalMap` with the vectors weighted by alpha.
    NormalizeNormalMapAlpha,
    /// `NormalizeNormalMapAlpha`-style normals plus noise in alpha. Not replicated.
    NormalizeNormalMapNoise,
    /// `NormalizeNormalMap`, then the xy part fades toward the flat normal on small mips.
    NormalizeNormalMapFade,
}

/// One TextureHints class with its inherited keys already resolved.
#[derive(Clone, Copy, Debug)]
pub struct Rule {
    /// Class name in the cfg.
    pub class: &'static str,
    /// Parent class (`class x: parent`), whose keys this entry already includes.
    pub parent: Option<&'static str>,
    /// The `name` wildcard, always `*_<suffix>.*`.
    pub pattern: &'static str,
    /// `format`; `None` means unset (DXT1/DXT5 or ARGB chosen from the image, see `enable_dxt`).
    pub format: Option<Fmt>,
    /// `enableDXT`.
    pub enable_dxt: Option<u8>,
    /// `channelSwizzleR/G/B/A` in that order; `None` = key absent (channel kept).
    pub swizzle: [Option<Src>; 4],
    /// `mipmapFilter`; `None` = unset.
    pub mip_filter: Option<Filter>,
    /// `dynRange`; `None` = unset. ImageToPAA treats unset as 1 (seen on `_gs`).
    pub dyn_range: Option<u8>,
    /// `autoreduce`.
    pub autoreduce: Option<u8>,
    /// `errorMetrics`.
    pub error_metrics: Option<&'static str>,
    /// True once the comparisons against ImageToPAA output passed for this suffix (see the module docs).
    pub verified: bool,
    /// Average the mip chain in linear light, weighting colour by alpha. ImageToPAA does this
    /// for `dynRange` 1 (`_8888`, `_co`, `_ca`) and when `dynRange` is unset (`_gs` and
    /// unsuffixed files). Plain sRGB averaging gave mips 2 and smaller that were 10 to 20 dB
    /// off its output. Mip 0 is not affected.
    pub linear_mips: bool,
}

impl Rule {
    /// The suffix without underscore and wildcards, e.g. `"nohq"` (empty for `PLAIN`).
    pub fn suffix(&self) -> &'static str {
        self.pattern
            .strip_prefix("*_")
            .and_then(|s| s.strip_suffix(".*"))
            .unwrap_or("")
    }

    /// True when any `channelSwizzle*` key is set (ImageToPAA then writes a SWIZ tag).
    pub fn has_swizzle(&self) -> bool {
        self.swizzle.iter().any(Option::is_some)
    }

    /// Why this crate cannot encode the rule, if it cannot: a mip filter whose noise comes from
    /// ImageToPAA's own generator. Such suffixes always route to ImageToPAA.
    pub fn unreplicable(&self) -> Option<&'static str> {
        match self.mip_filter {
            Some(Filter::AlphaNoise | Filter::AddAlphaNoise | Filter::NormalizeNormalMapNoise) => {
                Some("needs ImageToPAA: its mipmapFilter writes noise this encoder does not reproduce")
            }
            _ => None,
        }
    }

    /// The default pad colour (RGBA, source space, before any swizzle) for an output size that leaves
    /// canvas to fill (0.3.1, decision 51). The one place that says it:
    /// * normal maps (a normal-map mip filter): `128,128,255,255`, the flat normal;
    /// * ambient shadow (`_as`, `_ads`, `_adshq`): `255,255,255,255`, no shadow;
    /// * classes that keep alpha (a format with an alpha channel and the source alpha stored as alpha:
    ///   `_ca`, `_lca`, `_cat`, `_can`, `_8888`, `_mc`, `_pr`, `_gs`, `_raw` and the like): `0,0,0,0`;
    /// * everything else (including unsuffixed files): `0,0,0,255`.
    pub fn pad_colour(&self) -> [u8; 4] {
        let normal = matches!(
            self.mip_filter,
            Some(
                Filter::NormalizeNormalMap
                    | Filter::NormalizeNormalMapAlpha
                    | Filter::NormalizeNormalMapNoise
                    | Filter::NormalizeNormalMapFade
            )
        );
        let ambient = matches!(
            self.class,
            "ambient_shadow" | "ambient_diffuse_shadow" | "ambient_diffuse_shadow_hq"
        );
        let keeps_alpha = matches!(
            self.format,
            Some(Fmt::Dxt5 | Fmt::Argb1555 | Fmt::Argb4444 | Fmt::Ai88)
        ) && matches!(self.swizzle[3], None | Some(Src::A));
        if normal {
            [128, 128, 255, 255]
        } else if ambient {
            [255, 255, 255, 255]
        } else if keeps_alpha {
            [0, 0, 0, 0]
        } else {
            [0, 0, 0, 255]
        }
    }

    /// True when Loki's router may send this suffix here: verified and implemented.
    pub fn routable(&self) -> bool {
        self.verified && self.unreplicable().is_none()
    }
}

/// The rule for files without a suffix (and for suffixes no class names): the TexConvert default.
/// Not a cfg class. The format is DXT1 or DXT5 depending on the image's alpha, and the PAA FLAG
/// tag (which marks that the texture has transparency) follows the same alpha. Verified.
pub const PLAIN: Rule = Rule {
    class: "(none)",
    parent: None,
    pattern: "*",
    format: None,
    enable_dxt: None,
    swizzle: [None; 4],
    mip_filter: None,
    dyn_range: None,
    autoreduce: None,
    error_metrics: None,
    verified: true,
    linear_mips: true,
};

/// Starting point for every table entry: all keys unset and not verified. Each rule overrides
/// only what its cfg class sets.
const BASE: Rule = Rule {
    class: "",
    verified: false,
    linear_mips: false,
    ..PLAIN
};
use Filter as F;
use Fmt::*;
use Src::{InvA, InvG, InvR, One, Zero, A, B, G, R};

/// Every class of TexConvert.cfg 2.9.2014, in cfg order, inheritance resolved.
pub const RULES: &[Rule] = &[
    // ImageToPAA writes `_8888` files as ARGB1555, not 32-bit ARGB (observed), so the format
    // below is intentionally `Argb1555`.
    Rule {
        class: "TexRGBA8888",
        pattern: "*_8888.*",
        format: Some(Argb1555),
        dyn_range: Some(1),
        linear_mips: true,
        verified: true,
        ..BASE
    },
    Rule {
        class: "ColorMap",
        pattern: "*_co.*",
        format: Some(Dxt1),
        dyn_range: Some(1),
        verified: true,
        linear_mips: true,
        ..BASE
    },
    Rule {
        class: "ColorMapRaw",
        pattern: "*_raw.*",
        format: Some(Argb1555),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "ColorAlphaMap",
        pattern: "*_ca.*",
        format: Some(Dxt5),
        dyn_range: Some(1),
        verified: true,
        linear_mips: true,
        ..BASE
    },
    // Rules without `verified: true` are not routed here: `_cat`, `_non` and `_can` use noise
    // filters we cannot reproduce. (`_sky` failed the old absolute mip 2 floor; under the relative check
    // of #49 it passes, #31.)
    Rule {
        class: "ColorAlphaTest",
        pattern: "*_cat.*",
        format: Some(Dxt5),
        dyn_range: Some(1),
        mip_filter: Some(F::AlphaNoise),
        ..BASE
    },
    Rule {
        class: "sky",
        pattern: "*_sky.*",
        format: Some(Dxt5),
        swizzle: [Some(R), Some(InvA), Some(B), Some(InvG)],
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "detail",
        pattern: "*_detail.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        swizzle: [Some(A), Some(A), Some(A), Some(One)],
        dyn_range: Some(0),
        mip_filter: Some(F::FadeOut),
        verified: true,
        ..BASE
    },
    Rule {
        class: "color_detail",
        pattern: "*_cdt.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        dyn_range: Some(0),
        mip_filter: Some(F::FadeOut),
        verified: true,
        ..BASE
    },
    Rule {
        class: "layer_color",
        pattern: "*_lco.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        dyn_range: Some(0),
        autoreduce: Some(1),
        verified: true,
        ..BASE
    },
    Rule {
        class: "multiply_color",
        pattern: "*_mco.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        dyn_range: Some(0),
        autoreduce: Some(1),
        verified: true,
        ..BASE
    },
    Rule {
        class: "layer_color_draft",
        pattern: "*_draftlco.*",
        enable_dxt: Some(0),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "layer_color_alpha",
        pattern: "*_lca.*",
        format: Some(Dxt5),
        enable_dxt: Some(1),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "mask",
        pattern: "*_mask.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        dyn_range: Some(0),
        autoreduce: Some(1),
        verified: true,
        ..BASE
    },
    Rule {
        class: "prt",
        pattern: "*_pr.*",
        format: Some(Dxt5),
        enable_dxt: Some(1),
        dyn_range: Some(0),
        autoreduce: Some(1),
        verified: true,
        ..BASE
    },
    Rule {
        class: "ambient_diffuse_shadow",
        pattern: "*_ads.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        swizzle: [Some(One), Some(G), Some(B), Some(One)],
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "ambient_diffuse_shadow_hq",
        pattern: "*_adshq.*",
        format: Some(Dxt5),
        swizzle: [Some(Zero), Some(B), Some(Zero), Some(G)],
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "detail_specular_diffuseinverse_map",
        pattern: "*_dtsmdi.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        swizzle: [Some(R), Some(G), Some(B), Some(One)],
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "macro",
        pattern: "*_mc.*",
        format: Some(Dxt5),
        enable_dxt: Some(1),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "ambient_shadow",
        pattern: "*_as.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        swizzle: [Some(One), Some(G), Some(One), Some(One)],
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "specular_map",
        pattern: "*_sm.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        swizzle: [Some(R), Some(G), Some(B), Some(One)],
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "specular_diffuseinverse_map",
        pattern: "*_smdi.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        swizzle: [Some(One), Some(G), Some(B), Some(One)],
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "detail_short",
        parent: Some("detail"),
        pattern: "*_dt.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        swizzle: [Some(A), Some(A), Some(A), Some(One)],
        dyn_range: Some(0),
        mip_filter: Some(F::FadeOut),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap",
        pattern: "*_normalmap.*",
        enable_dxt: Some(1),
        mip_filter: Some(F::NormalizeNormalMapAlpha),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap_short",
        parent: Some("normalmap"),
        pattern: "*_no.*",
        format: Some(Dxt1),
        enable_dxt: Some(1),
        swizzle: [None, None, None, Some(One)],
        mip_filter: Some(F::NormalizeNormalMapAlpha),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap_uncompressed",
        parent: Some("normalmap"),
        pattern: "*_noex.*",
        enable_dxt: Some(0),
        mip_filter: Some(F::NormalizeNormalMapAlpha),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "NormalMapNoise",
        pattern: "*_non.*",
        format: Some(Dxt5),
        mip_filter: Some(F::NormalizeNormalMapNoise),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        ..BASE
    },
    Rule {
        class: "normalmap_hq",
        pattern: "*_nohq.*",
        format: Some(Dxt5),
        swizzle: [Some(InvA), Some(G), Some(B), Some(InvR)],
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        mip_filter: Some(F::NormalizeNormalMapAlpha),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap_vhq",
        pattern: "*_novhq.*",
        format: Some(Dxt5),
        swizzle: [Some(One), Some(G), Some(One), Some(InvR)],
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        mip_filter: Some(F::NormalizeNormalMapAlpha),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap_hq_fade",
        parent: Some("normalmap_hq"),
        pattern: "*_nofhq.*",
        format: Some(Dxt5),
        swizzle: [Some(InvA), Some(G), Some(B), Some(InvR)],
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        mip_filter: Some(F::NormalizeNormalMapFade),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmapFade",
        pattern: "*_nof.*",
        enable_dxt: Some(1),
        mip_filter: Some(F::NormalizeNormalMapFade),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmapFade_uncompressed",
        parent: Some("normalmapFade"),
        pattern: "*_nofex.*",
        enable_dxt: Some(0),
        mip_filter: Some(F::NormalizeNormalMapFade),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap_spec",
        pattern: "*_ns.*",
        enable_dxt: Some(1),
        mip_filter: Some(F::NormalizeNormalMap),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap_parallax",
        pattern: "*_nopx.*",
        format: Some(Dxt5),
        enable_dxt: Some(1),
        swizzle: [Some(A), Some(G), None, Some(InvR)],
        mip_filter: Some(F::NormalizeNormalMap),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap_spec_uncompressed",
        parent: Some("normalmap_spec"),
        pattern: "*_nsex.*",
        enable_dxt: Some(0),
        mip_filter: Some(F::NormalizeNormalMap),
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        verified: true,
        ..BASE
    },
    Rule {
        class: "normalmap_spec_hq",
        parent: Some("normalmap_hq"),
        pattern: "*_nshq.*",
        format: Some(Dxt5),
        swizzle: [Some(InvA), Some(G), Some(B), Some(InvR)],
        dyn_range: Some(0),
        error_metrics: Some("Distance"),
        mip_filter: Some(F::NormalizeNormalMapAlpha),
        verified: true,
        ..BASE
    },
    Rule {
        class: "grayscalealpha",
        pattern: "*_gs.*",
        format: Some(Ai88),
        linear_mips: true,
        verified: true,
        ..BASE
    },
    Rule {
        class: "AddAlphaNoise",
        pattern: "*_can.*",
        format: Some(Dxt5),
        mip_filter: Some(F::AddAlphaNoise),
        dyn_range: Some(1),
        error_metrics: Some("Distance"),
        ..BASE
    },
    Rule {
        class: "TexRGBA4444",
        pattern: "*_4444.*",
        format: Some(Argb4444),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "TexRGBA1555",
        pattern: "*_1555.*",
        format: Some(Argb1555),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "TexAI88",
        pattern: "*_88.*",
        format: Some(Ai88),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "TexDXT1",
        pattern: "*_dxt1.*",
        format: Some(Dxt1),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
    Rule {
        class: "TexDXT5",
        pattern: "*_dxt5.*",
        format: Some(Dxt5),
        dyn_range: Some(0),
        verified: true,
        ..BASE
    },
];

/// The class whose `name` matches the suffix (case-insensitive, no underscore), e.g. `find("nohq")`.
/// An empty suffix returns `None`; callers then use `PLAIN`.
pub fn find(suffix: &str) -> Option<&'static Rule> {
    if suffix.is_empty() {
        return None;
    }
    RULES
        .iter()
        .find(|r| r.suffix().eq_ignore_ascii_case(suffix))
}

/// The rule for a suffix: its class, else `PLAIN`. The one place that says "an unknown suffix gets
/// the default rule" (observed #054: ImageToPAA wrote the same type, tags and mips for `_foo`,
/// `_xyz`, `_co2`, `_nrm`, `_caa`, `_cox`, `_nohq2` and a name with no underscore).
pub fn for_suffix(suffix: &str) -> &'static Rule {
    find(suffix).unwrap_or(&PLAIN)
}

/// The table as text, one line per class, keys spelled as in the cfg. Meant to be diffed against
/// TexConvert.cfg by a script, and printed by `paa-proto rules`. Lines:
/// `# ...` comments, `convertVersion=N`, then `class=<name> parent=<name|-> key=value ...`
/// (`verified=` and `routable=` are our additions, not cfg keys).
pub fn table_text() -> String {
    let mut out = String::new();
    out.push_str("# our rule table; matches TexConvert.cfg 2.9.2014\nconvertVersion=6\n");
    out.push_str("# keys acted on: name format enableDXT channelSwizzleR channelSwizzleG channelSwizzleB channelSwizzleA mipmapFilter dynRange autoreduce\n");
    out.push_str("# keys recorded only: errorMetrics\n# unknown keys: none\n");
    for r in RULES {
        out.push_str(&format!(
            "class={} parent={} name={}",
            r.class,
            r.parent.unwrap_or("-"),
            r.pattern
        ));
        if let Some(f) = r.format {
            out.push_str(&format!(" format={}", f.cfg()));
        }
        if let Some(v) = r.enable_dxt {
            out.push_str(&format!(" enableDXT={v}"));
        }
        for (key, s) in ["R", "G", "B", "A"].iter().zip(r.swizzle) {
            if let Some(s) = s {
                out.push_str(&format!(" channelSwizzle{key}={}", s.cfg()));
            }
        }
        if let Some(v) = r.dyn_range {
            out.push_str(&format!(" dynRange={v}"));
        }
        if let Some(f) = r.mip_filter {
            out.push_str(&format!(" mipmapFilter={f:?}"));
        }
        if let Some(v) = r.autoreduce {
            out.push_str(&format!(" autoreduce={v}"));
        }
        if let Some(m) = r.error_metrics {
            out.push_str(&format!(" errorMetrics={m}"));
        }
        out.push_str(&format!(
            " verified={} routable={}\n",
            u8::from(r.verified),
            u8::from(r.routable())
        ));
    }
    out
}
