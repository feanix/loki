//! PAA container: reader and writer for the file layout.
//!
//! Plain container code only: it packs already-encoded mip data into a PAA file and unpacks
//! one again. It knows nothing about pixels, DXT blocks, LZSS or LZO; callers pass finished
//! bytes and the type word (`ty`, DXT or a 16-bit pixel format). Palettes are not supported.
//!
//! Layout (little endian): u16 type, then tags (each: the 4 bytes "GGAT", 4 name bytes stored
//! reversed, u32 length, data), u16 palette count (always 0), mip entries (u16 width, with
//! 0x8000 set if LZO-compressed; u16 height; 3-byte length; data), then 6 zero bytes. The
//! reader treats a mip entry with width and height both 0 as the end of the chain.
//! OFFS holds absolute offsets of the mip entries, so sizes are known before
//! writing and the file is emitted in one pass (no back-patching).

use crate::error::Error;

/// Type word of a DXT1 texture.
pub const TYPE_DXT1: u16 = 0xFF01;
/// Type word of a DXT5 texture.
pub const TYPE_DXT5: u16 = 0xFF05;

/// Largest stored mip size: the length field is 24 bits.
pub(crate) const MAX_MIP_LEN: usize = 0xFF_FFFF;

/// One mip level as stored in the file.
pub struct Mip {
    /// Width in pixels (at most 0x7FFF: the top bit of the stored word is the LZO flag).
    pub w: u32,
    /// Height in pixels (at most 0xFFFF).
    pub h: u32,
    /// Data is an LZO1X stream (width high bit set on disk).
    pub lzo: bool,
    /// Stored bytes (compressed if `lzo`).
    pub data: Vec<u8>,
}

/// A named tag (AVGC, MAXC, FLAG, ...).
pub struct Tag {
    /// Logical name, exactly 4 ASCII bytes, e.g. "AVGC" (stored byte-reversed on disk).
    pub name: String,
    /// Tag payload.
    pub data: Vec<u8>,
}

/// A parsed or to-be-written PAA file.
pub struct Paa {
    /// Type word: `TYPE_DXT1`, `TYPE_DXT5`, or a pixel-format word (0x1555, 0x4444, 0x8080).
    pub ty: u16,
    /// Tags except OFFS (computed on write, verified on read).
    pub tags: Vec<Tag>,
    /// Mip chain, largest first (at most 16).
    pub mips: Vec<Mip>,
}

/// The on-disk spelling of a 4-byte tag name: the bytes reversed.
fn rev4(s: &str) -> Result<[u8; 4], Error> {
    match s.as_bytes() {
        // ASCII only: the reader maps each stored byte to one char, so a multi-byte name would not
        // read back as written.
        [a, b, c, d] if s.is_ascii() => Ok([*d, *c, *b, *a]),
        _ => Err(Error::Paa(format!("tag name {s:?} is not 4 ASCII bytes"))),
    }
}

/// Serialise `p`. Fails (never panics) on a mip or tag that does not fit its field.
pub fn write(p: &Paa) -> Result<Vec<u8>, Error> {
    if p.mips.len() > 16 {
        return Err(Error::Paa("more than 16 mips".into()));
    }
    // OFFS is written below from the real mip positions; a second one from the caller would make
    // the file ambiguous (the reader keeps the last).
    if p.tags.iter().any(|t| t.name == "OFFS") {
        return Err(Error::Paa(
            "OFFS tag is computed by the writer, not passed in".into(),
        ));
    }
    // Each tag costs a 12-byte header ("GGAT", name, u32 length) plus its data.
    let tags_len: usize = p.tags.iter().map(|t| 12 + t.data.len()).sum();
    // The OFFS tag is always 16 offsets of 4 bytes (64 bytes) plus its 12-byte header; unused
    // slots stay 0.
    let offs_len = 12 + 64;
    // Offset of the first mip entry: type word (2), tags, OFFS tag, palette count (2). OFFS
    // stores absolute offsets, so every position must be known before writing.
    let first = 2 + tags_len + offs_len + 2;
    let mut offsets = [0u32; 16];
    let mut pos = first;
    for (i, m) in p.mips.iter().enumerate() {
        offsets[i] = u32::try_from(pos).map_err(|_| Error::Paa("file exceeds 4 GiB".into()))?;
        if m.data.len() > MAX_MIP_LEN {
            return Err(Error::MipTooLarge {
                w: m.w,
                h: m.h,
                len: m.data.len(),
            });
        }
        // A 0x0 entry is the end-of-chain marker, so the reader would stop there and drop the rest.
        if m.w == 0 && m.h == 0 {
            return Err(Error::Paa("a 0x0 mip would end the mip chain".into()));
        }
        if m.w > 0x7FFF || m.h > 0xFFFF {
            return Err(Error::Paa(format!(
                "mip {}x{} does not fit the 16-bit size fields",
                m.w, m.h
            )));
        }
        // A mip entry is a 7-byte header (width 2, height 2, length 3) plus its data.
        pos += 7 + m.data.len();
    }
    let mut out = Vec::with_capacity(pos + 6);
    out.extend_from_slice(&p.ty.to_le_bytes());
    let put_tag = |out: &mut Vec<u8>, name: &str, data: &[u8]| -> Result<(), Error> {
        let len =
            u32::try_from(data.len()).map_err(|_| Error::Paa("tag larger than 4 GiB".into()))?;
        out.extend_from_slice(b"GGAT");
        out.extend_from_slice(&rev4(name)?);
        out.extend_from_slice(&len.to_le_bytes());
        out.extend_from_slice(data);
        Ok(())
    };
    for t in &p.tags {
        put_tag(&mut out, &t.name, &t.data)?;
    }
    let offs: Vec<u8> = offsets.iter().flat_map(|o| o.to_le_bytes()).collect();
    put_tag(&mut out, "OFFS", &offs)?;
    // Palette count: always 0, since no palette formats are written.
    out.extend_from_slice(&0u16.to_le_bytes());
    for m in &p.mips {
        // Widths and heights were range-checked above, so these casts cannot truncate.
        let w = m.w as u16 | if m.lzo { 0x8000 } else { 0 };
        out.extend_from_slice(&w.to_le_bytes());
        out.extend_from_slice(&(m.h as u16).to_le_bytes());
        out.extend_from_slice(&(m.data.len() as u32).to_le_bytes()[..3]);
        out.extend_from_slice(&m.data);
    }
    // End of the mip chain: a mip entry with width 0 and height 0, padded to 6 bytes. The files
    // the game's own tools write end with the same 6 zero bytes.
    out.extend_from_slice(&[0u8; 6]);
    Ok(out)
}

/// Error for any read that runs past the end of the input.
fn truncated() -> Error {
    Error::Paa("truncated".into())
}

/// Little-endian u16 at byte offset `o`, or a truncation error.
fn u16_at(b: &[u8], o: usize) -> Result<u16, Error> {
    b.get(o..o + 2)
        .map(|s| u16::from_le_bytes([s[0], s[1]]))
        .ok_or_else(truncated)
}

/// Little-endian u32 at byte offset `o`, or a truncation error.
fn u32_at(b: &[u8], o: usize) -> Result<u32, Error> {
    b.get(o..o + 4)
        .map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]]))
        .ok_or_else(truncated)
}

/// Parse a PAA file (the layout `write` produces; palettes are refused).
pub fn read(b: &[u8]) -> Result<Paa, Error> {
    let ty = u16_at(b, 0)?;
    let mut pos = 2;
    let mut tags = Vec::new();
    let mut offs: Vec<u32> = Vec::new();
    // Phase 1: tags. Each starts with "GGAT"; the name is stored reversed. OFFS is parsed
    // separately and checked against the real mip positions below.
    while b.get(pos..pos + 4) == Some(b"GGAT") {
        let nm = b
            .get(pos + 4..pos + 8)
            .ok_or_else(|| Error::Paa("truncated tag".into()))?;
        let name: String = nm.iter().rev().map(|&c| c as char).collect();
        let len = u32_at(b, pos + 8)? as usize;
        let data = b
            .get(pos + 12..pos + 12 + len)
            .ok_or_else(|| Error::Paa("truncated tag data".into()))?
            .to_vec();
        pos += 12 + len;
        if name == "OFFS" {
            if data.len() % 4 != 0 {
                return Err(Error::Paa(format!(
                    "OFFS length {} is not a multiple of 4",
                    data.len()
                )));
            }
            offs = data
                .chunks_exact(4)
                .map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                .collect();
        } else {
            tags.push(Tag { name, data });
        }
    }
    let pal = u16_at(b, pos)?;
    if pal != 0 {
        return Err(Error::Paa("palette not supported".into()));
    }
    pos += 2;
    let mut mips = Vec::new();
    loop {
        let w = u16_at(b, pos)?;
        let h = u16_at(b, pos + 2)?;
        // A mip entry with width and height both 0 ends the chain.
        if w == 0 && h == 0 {
            break;
        }
        // The length is 3 bytes (24 bits), so it is assembled by hand.
        let len = *b.get(pos + 4).ok_or_else(truncated)? as usize
            | (*b.get(pos + 5).ok_or_else(truncated)? as usize) << 8
            | (*b.get(pos + 6).ok_or_else(truncated)? as usize) << 16;
        let data = b
            .get(pos + 7..pos + 7 + len)
            .ok_or_else(|| Error::Paa("truncated mip data".into()))?
            .to_vec();
        // OFFS must point exactly at each mip entry; a mismatch means a corrupt or foreign file.
        if offs.get(mips.len()).copied() != Some(pos as u32) {
            return Err(Error::Paa(format!(
                "OFFS[{}] does not match mip position {}",
                mips.len(),
                pos
            )));
        }
        mips.push(Mip {
            w: (w & 0x7FFF) as u32,
            h: h as u32,
            lzo: w & 0x8000 != 0,
            data,
        });
        pos += 7 + len;
    }
    Ok(Paa { ty, tags, mips })
}
