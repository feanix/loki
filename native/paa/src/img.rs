//! PNG input and output, and the 2x2 box filter used to build smaller mips.
//!
//! Decoding uses the general-purpose `png` crate; this file turns every PNG variant (palette,
//! grey, 16-bit, with or without alpha) into one internal form, 8-bit RGBA, so the encoder
//! handles a single pixel layout. The PNG header comes from a user file and is treated as
//! untrusted: sizes are checked before anything is allocated.

use crate::encode::MAX_DIM;
use crate::error::Error;
use std::fs::File;
use std::io::BufWriter;
use std::path::Path;

/// An RGBA8 image, row-major, 4 bytes per pixel.
pub struct Image {
    /// Width in pixels.
    pub w: usize,
    /// Height in pixels.
    pub h: usize,
    /// RGBA8, row-major; must hold exactly `w * h * 4` bytes (the encoder checks).
    pub rgba: Vec<u8>,
}

/// Decode a PNG file to RGBA8 (palette, grey and 16-bit inputs are normalised).
///
/// The header is untrusted: its size is rejected before any buffer is sized from it.
pub fn load_png(path: &Path) -> Result<Image, Error> {
    let file = File::open(path).map_err(|e| Error::io(path, e))?;
    let mut dec = png::Decoder::new(file);
    // EXPAND turns palette, low-bit-depth and tRNS-transparency images into plain 8-bit samples
    // (tRNS adds an alpha channel, which is why an RGB PNG can come out as Rgba); STRIP_16 drops
    // 16-bit samples to 8-bit. PAA texels are 8 bits per channel at most, so we never need more.
    dec.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    // Cap the decoder's own metadata allocations at the largest RGBA image we accept.
    let budget = MAX_DIM
        .checked_mul(MAX_DIM)
        .and_then(|n| n.checked_mul(4))
        .ok_or_else(|| Error::Png("size budget overflow".into()))?;
    dec.set_limits(png::Limits { bytes: budget });
    let mut rd = dec.read_info()?;
    // A 68-byte file can declare 200000x200000 and an allocation failure aborts the
    // process, so reject before any buffer is sized from the header.
    let hdr = rd.info();
    let (hw, hh) = (hdr.width as usize, hdr.height as usize);
    if hw == 0 || hh == 0 || hw > MAX_DIM || hh > MAX_DIM {
        return Err(Error::Png(format!("PNG size {hw}x{hh}: must be 1..={MAX_DIM} on each side")));
    }
    // Defensive: after the size check the output cannot exceed the budget for 8-bit samples, but
    // the buffer size is computed by the png crate, so verify it directly before allocating.
    let out_len = rd.output_buffer_size();
    if out_len > budget {
        return Err(Error::Png(format!("PNG decodes to {out_len} bytes, over the {budget} byte budget")));
    }
    let mut buf = vec![0; out_len];
    let info = rd.next_frame(&mut buf)?;
    let (w, h) = (info.width as usize, info.height as usize);
    let src = &buf[..info.buffer_size()];
    let mut rgba = Vec::with_capacity(w * h * 4);
    // Convert to RGBA8. EXPAND guarantees no indexed output, so that arm is an internal-error
    // guard. Grey becomes R=G=B=grey with full alpha; grey+alpha keeps its alpha; RGB gets 255.
    match info.color_type {
        png::ColorType::Rgba => rgba.extend_from_slice(src),
        png::ColorType::Rgb => src.chunks(3).for_each(|p| rgba.extend_from_slice(&[p[0], p[1], p[2], 255])),
        png::ColorType::Grayscale => src.iter().for_each(|&g| rgba.extend_from_slice(&[g, g, g, 255])),
        png::ColorType::GrayscaleAlpha => src.chunks(2).for_each(|p| rgba.extend_from_slice(&[p[0], p[0], p[0], p[1]])),
        png::ColorType::Indexed => return Err(Error::Png("indexed PNG was not expanded".into())),
    }
    Ok(Image { w, h, rgba })
}

/// Write an RGBA8 PNG (fast compression: used for scratch artefacts such as decoded mips).
pub fn save_png(path: &Path, img: &Image) -> Result<(), Error> {
    let (w, h) = (
        u32::try_from(img.w).map_err(|_| Error::Png("image too wide".into()))?,
        u32::try_from(img.h).map_err(|_| Error::Png("image too tall".into()))?,
    );
    let file = File::create(path).map_err(|e| Error::io(path, e))?;
    let mut enc = png::Encoder::new(BufWriter::new(file), w, h);
    enc.set_color(png::ColorType::Rgba);
    enc.set_depth(png::BitDepth::Eight);
    enc.set_compression(png::Compression::Fast);
    let mut wr = enc.write_header()?;
    wr.write_image_data(&img.rgba)?;
    Ok(())
}

/// 2x2 box filter with rounding. Both dimensions must be even and `rgba` must
/// hold `w * h * 4` bytes; the encoder checks both before it calls this.
pub(crate) fn halve(img: &Image) -> Image {
    let (w, h) = (img.w / 2, img.h / 2);
    let mut rgba = vec![0u8; w * h * 4];
    for y in 0..h {
        for x in 0..w {
            // s(dx, dy, c): channel c of the source texel at offset (dx, dy) inside the 2x2 block
            // that maps to output pixel (x, y).
            let s = |dx: usize, dy: usize, c: usize| img.rgba[((2 * y + dy) * img.w + 2 * x + dx) * 4 + c] as u32;
            // Average the four source texels per channel. Adding 2 before dividing by 4 rounds to
            // nearest (halves round up) instead of truncating. Alpha is averaged like the colour
            // channels, without premultiplying.
            for c in 0..4 {
                rgba[(y * w + x) * 4 + c] = ((s(0, 0, c) + s(1, 0, c) + s(0, 1, c) + s(1, 1, c) + 2) / 4) as u8;
            }
        }
    }
    Image { w, h, rgba }
}
