//! The one error type of the crate.
//!
//! Every fallible function returns `Result<_, Error>`. Display text is short and
//! user-presentable (no Debug output). The Python layer maps `Error::Io` to `OSError`,
//! `Error::Unsupported` to the `NeedsImageToPAA` exception (a `ValueError` subclass the caller
//! uses to fall back to ImageToPAA), and every other variant to `ValueError`.

use crate::paa::MAX_MIP_LEN;
use std::fmt;
use std::path::{Path, PathBuf};

/// Why an encode, read or decode failed.
#[derive(Debug)]
pub enum Error {
    /// A file could not be opened, read or written.
    Io {
        /// File the operation was on.
        path: PathBuf,
        /// The underlying OS error.
        source: std::io::Error,
    },
    /// The PNG is malformed, over a limit, or in an unsupported layout.
    Png(String),
    /// The image size cannot be encoded (not a power of two, too large, cannot be halved).
    Size(String),
    /// One mip's stored size does not fit the 24-bit length field of the PAA format.
    /// Callers can retry at half the size (an incompressible 4096x4096 `_ca` mip is 1 byte over).
    MipTooLarge {
        /// Mip width in pixels.
        w: u32,
        /// Mip height in pixels.
        h: u32,
        /// Stored size in bytes.
        len: usize,
    },
    /// LZO compression or decompression of a mip failed (LZO is the compression PAA applies to
    /// stored mips).
    Lzo(String),
    /// A .paa file is malformed or uses a feature this crate does not read.
    Paa(String),
    /// The file name asks for an encoding this crate does not implement.
    /// `reason` is the short label shown to the user ("noise mip filter"), `detail` the full text.
    Unsupported {
        reason: &'static str,
        detail: String,
    },
    /// Bad arguments: command line misuse in the `paa-proto` tool, or an input path with no
    /// usable file name in `encode_file`.
    Usage(String),
}

impl Error {
    /// Wrap an OS error with the path it happened on.
    pub fn io(path: &Path, source: std::io::Error) -> Error {
        Error::Io {
            path: path.to_path_buf(),
            source,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io { path, source } => write!(f, "{}: {source}", path.display()),
            Error::Png(m)
            | Error::Size(m)
            | Error::Lzo(m)
            | Error::Paa(m)
            | Error::Usage(m)
            | Error::Unsupported { detail: m, .. } => f.write_str(m),
            // MAX_MIP_LEN = 2^24 - 1 = 16777215, the largest value of the PAA mip length field.
            Error::MipTooLarge { w, h, len } => {
                write!(f, "mip {w}x{h} stored size {len} exceeds the {MAX_MIP_LEN} byte limit of the PAA format")
            }
        }
    }
}

// No `source()`: Display already prints the OS error, so a chain would show it twice.
impl std::error::Error for Error {}

impl From<png::DecodingError> for Error {
    fn from(e: png::DecodingError) -> Error {
        Error::Png(e.to_string())
    }
}

impl From<png::EncodingError> for Error {
    fn from(e: png::EncodingError) -> Error {
        Error::Png(e.to_string())
    }
}
