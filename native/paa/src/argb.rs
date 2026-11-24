//! The uncompressed pixel formats (ARGB1555, ARGB4444, AI88) and their LZSS wrapper.
//!
//! Layout as written by ImageToPAA (observed in its output, decoded and checked against the source
//! pixels): little-endian u16 per pixel, `A<<15|R<<10|G<<5|B` for 1555 and `A<<12|R<<8|G<<4|B`
//! for 4444; AI88 is one byte of BT.601 luma then one byte of alpha. Every mip is LZSS
//! compressed (the width's LZO bit stays clear) and ends with a 4-byte checksum: the little-endian
//! sum of the raw bytes taken as signed chars.
//!
//! LZSS: groups of 8 items led by a flag byte, least significant bit first. A set bit is one
//! literal byte; a clear bit is a 2-byte match `b0 = distance & 255`,
//! `b1 = (distance >> 4) & 0xF0 | (length - 3)` with distance 1..=4095 and length 3..=18, copied
//! byte by byte (so a match may overlap its own output). The compressor here is greedy over a
//! hash chain; its bytes need not equal ImageToPAA's, only decode to the same pixels.
//!
//! `unpack` and `lzss_unpack` are the reverse direction: the library's `decode_mip` uses them to
//! read these formats, and the tests use them to round-trip the encoder's output.

use crate::error::Error;
use crate::rules::Fmt;

/// Longest back reference (12 bits).
const MAX_DIST: usize = 4095;
/// Shortest match worth encoding: a match costs 2 bytes, so under 3 bytes it saves nothing.
const MIN_LEN: usize = 3;
/// Longest match: the length field is 4 bits, stored as `length - 3`, so 3..=18.
const MAX_LEN: usize = 18;
/// Hash chain entries tried per position (a speed against compression ratio trade-off).
const CHAIN: usize = 48;
/// log2 of the hash table size: 2^14 = 16384 buckets, indexed by a hash of 3 bytes.
const HASH_BITS: u32 = 14;

/// PAA type word of an uncompressed format.
pub(crate) fn type_word(f: Fmt) -> u16 {
    match f {
        Fmt::Argb1555 => 0x1555,
        Fmt::Argb4444 => 0x4444,
        Fmt::Ai88 => 0x8080,
        Fmt::Dxt1 | Fmt::Dxt5 => unreachable!("block formats have their own type words"),
    }
}

/// `v` (0..=255) rounded to `max` levels: round(v * max / 255).
fn q(v: u8, max: u32) -> u16 {
    ((v as u32 * max + 127) / 255) as u16
}

/// Pack RGBA8 pixels into the raw (not yet LZSS) bytes of `fmt`.
pub(crate) fn pack(rgba: &[u8], fmt: Fmt) -> Vec<u8> {
    let mut out = Vec::with_capacity(rgba.len() / 2);
    for p in rgba.chunks_exact(4) {
        match fmt {
            Fmt::Argb1555 => {
                // 1555 has a single alpha bit: 128 and above is opaque, below is transparent.
                let a = u16::from(p[3] >= 128);
                out.extend_from_slice(&((a << 15) | (q(p[0], 31) << 10) | (q(p[1], 31) << 5) | q(p[2], 31)).to_le_bytes());
            }
            Fmt::Argb4444 => {
                out.extend_from_slice(&((q(p[3], 15) << 12) | (q(p[0], 15) << 8) | (q(p[1], 15) << 4) | q(p[2], 15)).to_le_bytes());
            }
            Fmt::Ai88 => {
                // BT.601 luma, weights in thousandths (0.299, 0.587, 0.114); +500 rounds to nearest.
                let l = (299 * p[0] as u32 + 587 * p[1] as u32 + 114 * p[2] as u32 + 500) / 1000;
                out.extend_from_slice(&[l as u8, p[3]]);
            }
            Fmt::Dxt1 | Fmt::Dxt5 => unreachable!("block formats are not packed here"),
        }
    }
    out
}

/// Expand raw bytes of `fmt` back to RGBA8 (bit replication; AI88 grey into RGB).
pub(crate) fn unpack(raw: &[u8], fmt: Fmt) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw.len() * 2);
    // Widen 5 bits to 8 by repeating the top bits, so 31 maps to exactly 255. The 4-bit channels
    // below use `* 17` (0xF -> 0xFF) for the same reason.
    let e5 = |v: u16| ((v << 3) | (v >> 2)) as u8;
    for b in raw.chunks_exact(2) {
        let v = u16::from_le_bytes([b[0], b[1]]);
        match fmt {
            Fmt::Argb1555 => out.extend_from_slice(&[e5((v >> 10) & 31), e5((v >> 5) & 31), e5(v & 31), if v >> 15 != 0 { 255 } else { 0 }]),
            Fmt::Argb4444 => out.extend_from_slice(&[
                (((v >> 8) & 15) * 17) as u8,
                (((v >> 4) & 15) * 17) as u8,
                ((v & 15) * 17) as u8,
                (((v >> 12) & 15) * 17) as u8,
            ]),
            Fmt::Ai88 => out.extend_from_slice(&[b[0], b[0], b[0], b[1]]),
            Fmt::Dxt1 | Fmt::Dxt5 => unreachable!("block formats are not unpacked here"),
        }
    }
    out
}

/// The 4-byte trailer: signed-char sum of the raw bytes.
fn checksum(raw: &[u8]) -> u32 {
    // Each byte counts as a signed char (-128..=127), sign-extended to 32 bits, as the format's
    // checksum does. The wrapping add matches C integer overflow.
    raw.iter().fold(0u32, |s, &b| s.wrapping_add(b as i8 as i32 as u32))
}

/// Hash of the 3 bytes at `d[i..i + 3]`, used to find earlier positions with the same start.
/// Multiplying by 0x9E3779B1 (a golden-ratio constant) spreads the bits; the top `HASH_BITS`
/// bits of the product are the bucket.
fn hash3(d: &[u8], i: usize) -> usize {
    let v = (d[i] as u32) | ((d[i + 1] as u32) << 8) | ((d[i + 2] as u32) << 16);
    (v.wrapping_mul(0x9E37_79B1) >> (32 - HASH_BITS)) as usize
}

/// LZSS-compress `raw` and append the checksum.
///
/// Greedy matching, no lazy matching: at each position take the longest match found, else a
/// literal. `head[h]` is the latest position whose 3 bytes hash to `h`, and `prev[p]` the earlier
/// position with the same hash, so together they form chains; `usize::MAX` ends a chain. The flag
/// byte is reserved when the first item of its group is written, and its bits are set afterwards
/// for the literals. Every position inside a match is still inserted, so later matches can point
/// into it.
pub(crate) fn lzss_pack(raw: &[u8]) -> Vec<u8> {
    let n = raw.len();
    let mut out = Vec::with_capacity(n + n / 8 + 8);
    let mut head = vec![usize::MAX; 1 << HASH_BITS];
    let mut prev = vec![usize::MAX; n];
    let insert = |head: &mut Vec<usize>, prev: &mut Vec<usize>, i: usize| {
        if i + MIN_LEN <= n {
            let h = hash3(raw, i);
            prev[i] = head[h];
            head[h] = i;
        }
    };
    let (mut i, mut flag_at, mut bit) = (0usize, 0usize, 8u32);
    while i < n {
        if bit == 8 {
            flag_at = out.len();
            out.push(0);
            bit = 0;
        }
        // Longest match at i among the most recent candidates.
        let (mut best_len, mut best_dist) = (0usize, 0usize);
        if i + MIN_LEN <= n {
            let mut cand = head[hash3(raw, i)];
            let mut tries = 0;
            while cand != usize::MAX && i - cand <= MAX_DIST && tries < CHAIN {
                let max = MAX_LEN.min(n - i);
                let mut l = 0;
                while l < max && raw[cand + l] == raw[i + l] {
                    l += 1;
                }
                if l > best_len {
                    (best_len, best_dist) = (l, i - cand);
                }
                cand = prev[cand];
                tries += 1;
            }
        }
        if best_len >= MIN_LEN {
            out.push((best_dist & 255) as u8);
            out.push((((best_dist >> 4) & 0xF0) | (best_len - MIN_LEN)) as u8);
            for k in 0..best_len {
                insert(&mut head, &mut prev, i + k);
            }
            i += best_len;
        } else {
            out[flag_at] |= 1 << bit;
            out.push(raw[i]);
            insert(&mut head, &mut prev, i);
            i += 1;
        }
        bit += 1;
    }
    out.extend_from_slice(&checksum(raw).to_le_bytes());
    out
}

/// Inverse of `lzss_pack`: `want` raw bytes, with the checksum verified.
pub(crate) fn lzss_unpack(data: &[u8], want: usize) -> Result<Vec<u8>, Error> {
    let bad = |m: &str| Error::Paa(format!("lzss: {m}"));
    let body = data.len().checked_sub(4).ok_or_else(|| bad("shorter than the checksum"))?;
    let mut out: Vec<u8> = Vec::with_capacity(want);
    let mut i = 0;
    // Reads are limited to `body` (`i < body`, `i + 1 < body`) so the trailing 4-byte checksum is
    // never taken for data.
    while out.len() < want {
        let flags = *data.get(i).filter(|_| i < body).ok_or_else(|| bad("truncated"))?;
        i += 1;
        for b in 0..8 {
            if out.len() >= want {
                break;
            }
            if flags & (1 << b) != 0 {
                out.push(*data.get(i).filter(|_| i < body).ok_or_else(|| bad("truncated literal"))?);
                i += 1;
            } else {
                let (b0, b1) = match (data.get(i), data.get(i + 1)) {
                    (Some(&x), Some(&y)) if i + 1 < body => (x as usize, y as usize),
                    _ => return Err(bad("truncated match")),
                };
                i += 2;
                let dist = b0 | ((b1 & 0xF0) << 4);
                let len = (b1 & 15) + MIN_LEN;
                if dist == 0 {
                    return Err(bad("zero distance"));
                }
                for _ in 0..len {
                    // A match reaching before the start of the output reads the initial window,
                    // which is filled with spaces (0x20), as in the original LZSS. A valid
                    // encoder (such as `lzss_pack`) never emits such a match.
                    let v = if dist <= out.len() { out[out.len() - dist] } else { 0x20 };
                    out.push(v);
                }
            }
        }
    }
    // The final match may run past `want`; drop the excess.
    out.truncate(want);
    let want_sum = u32::from_le_bytes([data[body], data[body + 1], data[body + 2], data[body + 3]]);
    if checksum(&out) != want_sum {
        return Err(bad("checksum mismatch"));
    }
    Ok(out)
}
