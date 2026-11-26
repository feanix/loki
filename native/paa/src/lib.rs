//! loki_paa: encoder for PAA, the texture format used by Arma and DayZ, written for the Loki
//! texture converter.
//!
//! ImageToPAA is the official Bohemia tool that Loki otherwise calls. This crate reproduces its
//! output for the file-name suffixes (such as `_co`, `_nohq`) listed in TexConvert.cfg, the
//! tool's suffix-to-format table. Loki sends a file here only when its suffix has been checked
//! against ImageToPAA output; all other files still go to ImageToPAA.
//!
//! The library is single-threaded: it builds no thread pool, and the Python layer
//! releases the GIL for a whole encode so Loki's own worker pool provides the
//! parallelism. The main entry points are `supported` and `encode_file`. The lower-level steps
//! (`classify`, `load_png`, `limit_size`, `encode`) and the rule table in `rules` are public so
//! the `paa-proto` command line tool and tests can use them. The PAA reader and DXT decoder
//! (`read`, `decode_image`, `decode_mip`) exist only for that tool and for tests; Loki itself
//! never decodes.
//!
//! With the `python` feature the crate is also the CPython extension module `_paa`.

// Private modules hold the implementation (argb: 16-bit pixel formats, avgc: the AVGC per-texel
// colour table, cutout: the DXT1 1-bit alpha mask, dxt: block compression and decoding, encode: format choice and the encoder, img: PNG I/O, mipfilter: mip reduction
// filters, paa: file reader and writer). `rules` is public because it is the per-suffix table.
// Modules gated by cfg(test) are test code only.
mod argb;
mod avgc;
mod cutout;
mod dxt;
mod encode;
mod error;
#[cfg(test)]
mod golden;
mod img;
mod mipfilter;
mod noise;
mod noise_profile;
mod paa;
#[cfg(feature = "python")]
mod python;
mod resize;
pub mod rules;
#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_alpha;
#[cfg(test)]
mod tests_gaps;
#[cfg(test)]
mod tests_noise;
#[cfg(test)]
mod tests_rules;
#[cfg(test)]
mod tests_size;
#[cfg(test)]
mod tests_small;

pub use dxt::decode_image;
pub use encode::{
    classify, default_pad_colour, encode, encode_file, encode_file_sized, fit_png, limit_size,
};
pub use error::Error;
pub use img::{load_png, save_png, Image, MAX_DIM};
pub use paa::{read, Mip, Paa, Tag, TYPE_DXT1, TYPE_DXT5};
pub use resize::{fit_image, Align, Fit, SizeSpec};
pub use rules::{Filter, Fmt, Rule, Src};

/// Decode the raw (LZO already undone) bytes of a mip of any type this crate writes to RGBA8.
/// `ty` is the PAA type word; the pixel formats expect their LZSS stream, DXT the block data.
pub fn decode_mip(ty: u16, data: &[u8], w: usize, h: usize) -> Result<Vec<u8>, Error> {
    // The three non-DXT formats store 16 bits per pixel (hence w * h * 2 bytes after LZSS
    // decompression), so the unpack step is shared.
    // Checked: w and h come from the file header, and a 32-bit usize would wrap the product.
    let pixel = |f: Fmt| {
        let want = w
            .checked_mul(h)
            .and_then(|n| n.checked_mul(2))
            .ok_or_else(|| Error::Paa(format!("mip {w}x{h} is too large")))?;
        argb::lzss_unpack(data, want).map(|raw| argb::unpack(&raw, f))
    };
    match ty {
        TYPE_DXT1 => decode_image(data, w, h, false),
        TYPE_DXT5 => decode_image(data, w, h, true),
        // PAA type words: 0x1555 = ARGB1555, 0x4444 = ARGB4444, 0x8080 = AI88 (grey + alpha).
        0x1555 => pixel(Fmt::Argb1555),
        0x4444 => pixel(Fmt::Argb4444),
        0x8080 => pixel(Fmt::Ai88),
        t => Err(Error::Paa(format!("unsupported type 0x{t:04X}"))),
    }
}

/// Bytes of a DXT mip as raw blocks, undoing the LZO layer when the mip is flagged. `dxt5` picks
/// the block size. Not for the pixel formats (their mips are LZSS, see `decode_mip`).
///
/// The raw size is block based: DXT stores whole 4x4 texel blocks, 8 bytes each for DXT1 and 16
/// for DXT5, so a mip under 4x4 still holds one block per started 4 texels. LZO needs the exact
/// output size, which doubles as a corruption check; the decode is bounded to it, so a hostile
/// stream cannot expand past the header's claim (an LZO bomb).
pub fn mip_raw(m: &Mip, dxt5: bool) -> Result<std::borrow::Cow<'_, [u8]>, Error> {
    if !m.lzo {
        return Ok(std::borrow::Cow::Borrowed(&m.data));
    }
    let (w, h) = (m.w as usize, m.h as usize);
    let block = if dxt5 { 16 } else { 8 };
    // At most 0x7FFF * 0x3FFF blocks * 16 bytes: fits usize on 32 bit too, but stay checked.
    let want = w
        .div_ceil(4)
        .max(1)
        .checked_mul(h.div_ceil(4).max(1))
        .and_then(|n| n.checked_mul(block))
        .ok_or_else(|| Error::Lzo(format!("lzo decode {w}x{h}: size overflows")))?;
    let raw = lzokay_native::decompress_all_bounded(&m.data, want)
        .map_err(|e| Error::Lzo(format!("lzo decode {w}x{h}: {e}")))?;
    if raw.len() != want {
        return Err(Error::Lzo(format!(
            "lzo decode {w}x{h}: got {} bytes, expected {want}",
            raw.len()
        )));
    }
    Ok(std::borrow::Cow::Owned(raw))
}

/// True when Loki's router may send the PNG file `name` (e.g. `"rock_co.png"`) to this encoder:
/// the stem has no suffix or a suffix no class names (both get the default rule), or its
/// suffix names a class of the rule table that is verified against ImageToPAA and implemented
/// (`Rule::routable`); every class of the table is, the noise classes included. Every other name
/// (not a .png) is False; the caller sends those files to ImageToPAA. A trailing `.png` (any case) is ignored; so is any directory part
/// of `name`. `encode_file` is not gated by this: it also encodes the classes that are not yet
/// verified, which Loki does not route here.
pub fn supported(name: &str) -> bool {
    // Take the part after the last / or \. Strip a trailing .png (any case). Slicing at len - 4
    // would panic if it cut a multi-byte character, hence the is_char_boundary test; such a
    // name has no .png ending anyway.
    let file = name.rsplit(['/', '\\']).next().unwrap_or(name);
    let stem = match file.len().checked_sub(4) {
        Some(n) if file.is_char_boundary(n) && file[n..].eq_ignore_ascii_case(".png") => &file[..n],
        // No .png ending: a dot in the part after the last underscore is another extension
        // (`rock_co.jpg`), which is not ours. Without it an unknown suffix would claim it.
        _ if file
            .rsplit('_')
            .next()
            .is_some_and(|tail| tail.contains('.')) =>
        {
            return false
        }
        _ => file,
    };
    encode::stem_supported(stem)
}
