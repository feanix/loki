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

// Private modules hold the implementation (argb: 16-bit pixel formats, dxt: block compression
// and decoding, encode: format choice and the encoder, img: PNG I/O, mipfilter: mip reduction
// filters, paa: file reader and writer). `rules` is public because it is the per-suffix table.
// Modules gated by cfg(test) are test code only.
mod argb;
mod dxt;
mod encode;
mod error;
mod img;
mod mipfilter;
mod paa;
#[cfg(feature = "python")]
mod python;
pub mod rules;
#[cfg(test)]
mod golden;
#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_rules;

pub use dxt::decode_image;
pub use encode::{classify, encode, encode_file, limit_size, MAX_DIM};
pub use error::Error;
pub use img::{load_png, save_png, Image};
pub use paa::{read, Mip, Paa, Tag, TYPE_DXT1, TYPE_DXT5};
pub use rules::{Filter, Fmt, Rule, Src};

/// Decode the raw (LZO already undone) bytes of a mip of any type this crate writes to RGBA8.
/// `ty` is the PAA type word; the pixel formats expect their LZSS stream, DXT the block data.
pub fn decode_mip(ty: u16, data: &[u8], w: usize, h: usize) -> Result<Vec<u8>, Error> {
    // The three non-DXT formats store 16 bits per pixel (hence w * h * 2 bytes after LZSS
    // decompression), so the unpack step is shared.
    let pixel = |f: Fmt| argb::lzss_unpack(data, w * h * 2).map(|raw| argb::unpack(&raw, f));
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

/// True when Loki's router may send the PNG file `name` (e.g. `"rock_co.png"`) to this encoder:
/// the stem has no suffix, or its suffix names a class of the rule table that is verified against
/// ImageToPAA and implemented (`Rule::routable`). Every other name is False; the caller sends
/// those files to ImageToPAA. A trailing `.png` (any case) is ignored; so is any directory part
/// of `name`. `encode_file` is not gated by this: it also encodes the classes that are not yet
/// verified, which Loki does not route here.
pub fn supported(name: &str) -> bool {
    // Take the part after the last / or \. Strip a trailing .png (any case). Slicing at len - 4
    // would panic if it cut a multi-byte character, hence the is_char_boundary test; such a
    // name has no .png ending anyway.
    let file = name.rsplit(['/', '\\']).next().unwrap_or(name);
    let stem = match file.len().checked_sub(4) {
        Some(n) if file.is_char_boundary(n) && file[n..].eq_ignore_ascii_case(".png") => &file[..n],
        _ => file,
    };
    encode::stem_supported(stem)
}
