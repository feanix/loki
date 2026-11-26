//! PNG input and output, and the 2x2 box filter used to build smaller mips.
//!
//! Decoding uses the general-purpose `png` crate; this file turns every PNG variant (palette,
//! grey, 16-bit, with or without alpha) into one internal form, 8-bit RGBA, so the encoder
//! handles a single pixel layout. The PNG header comes from a user file and is treated as
//! untrusted: sizes are checked before anything is allocated.

use crate::error::Error;
use std::fs::File;
use std::io::BufWriter;
use std::path::Path;

/// Largest image side the encoder writes (the PAA mip chain starts at most this big). Larger PNGs
/// are halved to fit first, as ImageToPAA does (`encode_file`).
pub const MAX_DIM: usize = 4096;

/// Largest PNG side the loader accepts. 16384x1024 is a real case (ImageToPAA halves it to 4096x256).
pub const MAX_LOAD_SIDE: usize = 16384;

/// Largest PNG pixel count the loader accepts: 8192x8192, 256 MiB as RGBA8. This bounds memory (about
/// 320 MiB peak for an RGBA PNG, the decode buffer becomes the pixel buffer) and stays under the 89
/// million pixels at which Pillow starts warning about a decompression bomb when the app reads the header.
pub const MAX_LOAD_PIXELS: usize = 8192 * 8192;

/// An RGBA8 image, row-major, 4 bytes per pixel.
pub struct Image {
    /// Width in pixels.
    pub w: usize,
    /// Height in pixels.
    pub h: usize,
    /// RGBA8, row-major; must hold exactly `w * h * 4` bytes (the encoder checks).
    pub rgba: Vec<u8>,
}

impl Image {
    /// Err(`Size`) unless `rgba` holds exactly `w * h * 4` bytes. The one place the buffer length
    /// is checked: `encode` and `limit_size` call it before they index the pixels.
    pub fn check(&self) -> Result<(), Error> {
        if self.w.checked_mul(self.h).and_then(|n| n.checked_mul(4)) != Some(self.rgba.len()) {
            return Err(Error::Size(format!(
                "size {}x{}: pixel buffer has {} bytes",
                self.w,
                self.h,
                self.rgba.len()
            )));
        }
        Ok(())
    }
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
    let budget = MAX_LOAD_PIXELS * 4;
    dec.set_limits(png::Limits { bytes: budget });
    let mut rd = dec.read_info()?;
    // A 68-byte file can declare 200000x200000 and an allocation failure aborts the
    // process, so reject before any buffer is sized from the header.
    let hdr = rd.info();
    let (hw, hh) = (hdr.width as usize, hdr.height as usize);
    // No zero test: the png crate already refuses a 0-sized header in `read_info` ("Invalid image
    // dimensions"), so such a check here could never run.
    if hw > MAX_LOAD_SIDE || hh > MAX_LOAD_SIDE || hw * hh > MAX_LOAD_PIXELS {
        return Err(Error::Png(format!(
            "PNG size {hw}x{hh} is too big to load (at most {MAX_LOAD_SIDE} on a side and 8192x8192 pixels in all): scale it to 8192 or less in your image editor"
        )));
    }
    // A PLTE whose length is not a multiple of 3 makes the png crate panic while expanding (0.17.16,
    // transform/palette.rs), even when every index is 0; ImageToPAA rejects it too.
    if let Some(pal) = &hdr.palette {
        if pal.len() % 3 != 0 {
            return Err(Error::Png(format!(
                "invalid PNG palette: PLTE length {} is not a multiple of 3",
                pal.len()
            )));
        }
    }
    // Defensive: after the size check the output cannot exceed the budget for 8-bit samples, but
    // the buffer size is computed by the png crate, so verify it directly before allocating.
    let out_len = rd.output_buffer_size();
    if out_len > budget {
        return Err(Error::Png(format!(
            "PNG decodes to {out_len} bytes, over the {budget} byte budget"
        )));
    }
    let mut buf = vec![0; out_len];
    let info = rd.next_frame(&mut buf)?;
    let (w, h) = (info.width as usize, info.height as usize);
    buf.truncate(info.buffer_size());
    // Convert to RGBA8. EXPAND guarantees no indexed output, so that arm is an internal-error
    // guard. Grey becomes R=G=B=grey with full alpha; grey+alpha keeps its alpha; RGB gets 255.
    // RGBA is already in its final layout, so the decode buffer is kept as the pixel buffer
    // (an 8192x8192 PNG is not held twice).
    let rgba = match info.color_type {
        png::ColorType::Rgba => buf,
        png::ColorType::Rgb => convert(&buf, 3, w * h, |p, o| {
            o.extend_from_slice(&[p[0], p[1], p[2], 255])
        }),
        png::ColorType::Grayscale => convert(&buf, 1, w * h, |p, o| {
            o.extend_from_slice(&[p[0], p[0], p[0], 255])
        }),
        png::ColorType::GrayscaleAlpha => convert(&buf, 2, w * h, |p, o| {
            o.extend_from_slice(&[p[0], p[0], p[0], p[1]])
        }),
        png::ColorType::Indexed => return Err(Error::Png("indexed PNG was not expanded".into())),
    };
    Ok(Image { w, h, rgba })
}

/// Expand `n` source pixels of `step` bytes each to RGBA8 with `put` (the one pixel-conversion loop).
fn convert(src: &[u8], step: usize, n: usize, put: impl Fn(&[u8], &mut Vec<u8>)) -> Vec<u8> {
    let mut out = Vec::with_capacity(n * 4);
    src.chunks_exact(step).for_each(|p| put(p, &mut out));
    out
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

/// The four source texels of output texel (x, y) of the half-size level, in the order (0,0),
/// (1,0), (0,1), (1,1) of the 2x2 block.
fn quad(img: &Image, x: usize, y: usize) -> [[u8; 4]; 4] {
    let mut px = [[0u8; 4]; 4];
    for (i, (dx, dy)) in [(0, 0), (1, 0), (0, 1), (1, 1)].into_iter().enumerate() {
        let o = ((2 * y + dy) * img.w + 2 * x + dx) * 4;
        px[i].copy_from_slice(&img.rgba[o..o + 4]);
    }
    px
}

/// The one halver: a level of half the size (each side `/ 2`, so an odd side drops its last row or
/// column) in which output texel (x, y) is `texel(quad)` of the source 2x2 block. Every halving rule
/// in the crate (this file's box filter and the two chain filters in `mipfilter`) is a `texel`
/// closure. `rgba` must hold `w * h * 4` bytes (`Image::check`). An image with a side of 1 cannot
/// be halved and is returned as a copy, never as a 0-sized image.
pub(crate) fn halve_with(img: &Image, texel: impl Fn(&[[u8; 4]; 4]) -> [u8; 4]) -> Image {
    if img.w < 2 || img.h < 2 {
        return Image {
            w: img.w,
            h: img.h,
            rgba: img.rgba.clone(),
        };
    }
    let (w, h) = (img.w / 2, img.h / 2);
    let mut rgba = vec![0u8; w * h * 4];
    for y in 0..h {
        for x in 0..w {
            rgba[(y * w + x) * 4..][..4].copy_from_slice(&texel(&quad(img, x, y)));
        }
    }
    Image { w, h, rgba }
}
