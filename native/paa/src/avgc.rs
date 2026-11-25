//! The AVGC (average colour) value of ImageToPAA for the classes with `dynRange` 1 or unset.
//!
//! Observed: ImageToPAA does not average the plain
//! colour of a texel. It first turns every texel (colour byte `c`, alpha byte `a`) into a stored
//! colour `F(c, a)`, then takes the mean of those in LINEAR light weighted by alpha, and converts the
//! mean back to sRGB. `F` is the premultiplied byte `P = floor(c * a / 255)` taken back to linear
//! light and divided by the alpha (`lin(P) / (a / 255)`), with a floor near black that the
//! oracle adds and that falls away as `P` grows; for `a = 255` it is `c` except for the darkest
//! bytes (0 gives 1). The floor is not a closed formula that was found, so `F` is a measured table
//! (`avgc_soft.bin`, from 65,536 flat-image runs of the oracle, packed by `alpha31.py pack`).
//! It depends only on `(a, P)`, so the table holds one byte per `(a, P)`: 32,896 bytes.
//!
//! Checked against the oracle on flat colours x every alpha, on mixes of two texel kinds (the mean
//! is alpha weighted over texels), on generated soft-alpha art and on real soft-alpha textures:
//! every channel within 1 level.

/// `F` for `a` in 0..=255 and `P` in 0..=`a`, at index `a * (a + 1) / 2 + P`.
static TABLE: &[u8; 32896] = include_bytes!("avgc_soft.bin");

/// The stored sRGB byte `F(c, a)` the oracle averages for a texel of colour byte `c` and alpha `a`.
pub(crate) fn soft_colour(c: u8, a: u8) -> u8 {
    let (c, a) = (c as usize, a as usize);
    // Truncated, as the premultiplied byte is (checked on every (c, a): F is constant over each P).
    let p = c * a / 255;
    TABLE[a * (a + 1) / 2 + p]
}
