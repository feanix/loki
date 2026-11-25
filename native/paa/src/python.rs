//! The CPython extension module `_paa` (feature `python`).
//!
//! `supported(name)` says whether Loki may send a PNG file name to this encoder, and
//! `encode(png, paa, max_size, size, fit, pad_colour, align)` converts one PNG to PAA (`size` is an
//! output size, see `resize.rs`). The routing rule lives in Rust so Python does not duplicate it.
//! `fit_png` writes the same sized pixels as a PNG for an ImageToPAA hand-over, and
//! `default_pad_colour(name)` gives the class's default pad colour.
//!   supported(name: str) -> bool
//!   encode(png: str, paa: str, max_size: int | None, size: (int, int) | None, fit: str,
//!          pad_colour: (r, g, b, a) | None, align: str) -> None
//!   fit_png(png: str, out: str, size: (int, int), fit: str, pad_colour: ..., align: str) -> None
//!   default_pad_colour(name: str) -> (r, g, b, a)
//! `encode` raises `NeedsImageToPAA` (a ValueError) when this encoder refuses the file and the
//! caller should use ImageToPAA instead, `MipTooLarge` (a ValueError) when a mip overflows the
//! 24-bit length field (no hand-over: ImageToPAA wraps that field), ValueError for other bad input, OSError for file
//! problems, and RuntimeError if the encoder panicked.
//! `encode` releases the GIL for the whole encode and turns a Rust panic into
//! RuntimeError, so a bug in the encoder cannot kill the interpreter. (That needs
//! `panic = "unwind"`, the default; Cargo.toml must never set "abort".)

use crate::Error;
use pyo3::create_exception;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;

create_exception!(
    _paa,
    NeedsImageToPAA,
    PyValueError,
    "The encoder refuses this file (message = short reason, e.g. 'noise mip filter'): ImageToPAA must convert it."
);

create_exception!(
    _paa,
    MipTooLarge,
    PyValueError,
    "A mip's stored size does not fit the 24-bit length field of the PAA format (an incompressible 4096x4096 DXT5)."
);

/// Build the OSError a Python `open()` would raise: errno, strerror, filename and (Windows) winerror.
/// Calling the type with these arguments lets Python pick the subclass (FileNotFoundError,
/// PermissionError, ...), and `core.is_lock_error` reads winerror/errno, so a lock inside
/// `_paa.encode` is seen as a lock. On Windows `raw_os_error` is the Win32 code: it goes in
/// the winerror slot and Python derives errno from it.
fn io_to_py(path: PathBuf, source: &std::io::Error) -> PyErr {
    // Rust appends " (os error N)" to the OS text; Python's strerror has no such suffix.
    let text = source.to_string();
    let strerror = match text.rfind(" (os error ") {
        Some(i) => text[..i].to_string(),
        None => text,
    };
    // A str like Python's own open() reports (a PathBuf would arrive as a pathlib object).
    let path = path.to_string_lossy().into_owned();
    match source.raw_os_error() {
        #[cfg(windows)]
        Some(code) => PyOSError::new_err((None::<i32>, strerror, path, code)),
        #[cfg(not(windows))]
        Some(code) => PyOSError::new_err((code, strerror, path)),
        // No OS code (e.g. UnexpectedEof): errno stays None, the filename is still kept.
        None => PyOSError::new_err((None::<i32>, strerror, path)),
    }
}

/// Map a crate error to the Python exception the caller sees: a file the encoder refuses
/// (`Unsupported`) to `NeedsImageToPAA`, `MipTooLarge` to its own `ValueError` subclass, I/O errors
/// to `OSError`, everything else to `ValueError`.
/// `NeedsImageToPAA` carries only the short reason, because the caller uses it as a routing
/// signal and not as a user-facing message.
fn to_py(e: Error) -> PyErr {
    match e {
        Error::Unsupported { reason, .. } => NeedsImageToPAA::new_err(reason),
        Error::MipTooLarge { .. } => MipTooLarge::new_err(e.to_string()),
        Error::Io { path, source } => io_to_py(path, &source),
        _ => PyValueError::new_err(e.to_string()),
    }
}

/// Run `f` with the GIL released; an `Error` becomes ValueError/OSError, a panic RuntimeError.
/// AssertUnwindSafe is fine here: after a panic the closure's state is dropped and never
/// reused, so no broken state can be observed. `T: Send` is required because the work runs
/// with the GIL released.
fn guarded<T: Send>(py: Python<'_>, f: impl FnOnce() -> Result<T, Error> + Send) -> PyResult<T> {
    match py.detach(|| catch_unwind(AssertUnwindSafe(f))) {
        Ok(r) => r.map_err(to_py),
        Err(payload) => {
            // Keep the panic text (a bug inside a dependency such as the png crate is otherwise
            // unreportable); payloads are &str or String for panic!() without and with arguments.
            let msg = payload
                .downcast_ref::<&str>()
                .map(|s| s.to_string())
                .or_else(|| payload.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "no message".to_string());
            Err(PyRuntimeError::new_err(format!(
                "internal error in the PAA encoder (panic): {msg}"
            )))
        }
    }
}

/// True when Loki may route this PNG file name here: no suffix, an unknown suffix (default rule), or a verified rule-table suffix.
#[pyfunction]
fn supported(name: &str) -> bool {
    crate::supported(name)
}

/// The output size options as Python passes them (`size` is (width, height); `fit` and `align` are the
/// command line spellings; `pad_colour` is (r, g, b, a) or None for the class default). Bad words and
/// out-of-range numbers are a ValueError; whether the size is a legal output size is `fit_image`'s call.
fn size_spec(
    size: (i64, i64),
    fit: &str,
    pad_colour: Option<(u8, u8, u8, u8)>,
    align: &str,
) -> PyResult<crate::SizeSpec> {
    let side = |v: i64| {
        usize::try_from(v).map_err(|_| {
            PyValueError::new_err(format!("output size side must be positive, got {v}"))
        })
    };
    Ok(crate::SizeSpec {
        w: side(size.0)?,
        h: side(size.1)?,
        fit: crate::Fit::parse(fit).ok_or_else(|| {
            PyValueError::new_err(format!("fit must be stretch, contain or pad, got {fit}"))
        })?,
        colour: pad_colour.map(|(r, g, b, a)| [r, g, b, a]),
        align: crate::Align::parse(align)
            .ok_or_else(|| PyValueError::new_err(format!("unknown alignment {align}")))?,
    })
}

/// Encode `png` to `paa`, halving while the longest side exceeds `max_size` (None = no limit), or, with
/// `size`, fitting it to that output size (`max_size` must then be None).
#[pyfunction]
#[pyo3(signature = (png, paa, max_size=None, size=None, fit="contain", pad_colour=None, align="centre"))]
#[allow(clippy::too_many_arguments)] // one Python signature: the keywords are the API
fn encode(
    py: Python<'_>,
    png: PathBuf,
    paa: PathBuf,
    max_size: Option<i64>,
    size: Option<(i64, i64)>,
    fit: &str,
    pad_colour: Option<(u8, u8, u8, u8)>,
    align: &str,
) -> PyResult<()> {
    // max_size arrives as i64 so negative and huge values reach us and can be reported.
    // Values below 1 are rejected because no image can be halved to fit them.
    let max = match max_size {
        None => None,
        Some(m) if m >= 1 => {
            Some(usize::try_from(m).map_err(|_| PyValueError::new_err("max_size too large"))?)
        }
        Some(m) => {
            return Err(PyValueError::new_err(format!(
                "max_size must be at least 1, got {m}"
            )))
        }
    };
    let spec = match size {
        None => None,
        Some(_) if max.is_some() => {
            return Err(PyValueError::new_err(
                "size and max_size cannot be used together",
            ))
        }
        Some(s) => Some(size_spec(s, fit, pad_colour, align)?),
    };
    guarded(py, || {
        crate::encode_file_sized(&png, &paa, max, spec.as_ref())
    })
}

/// Write `png` at the output size as an 8-bit RGBA PNG at `out` (the pixels `encode` would encode), for
/// an ImageToPAA hand-over. The rule (mip filter, default pad colour) comes from the name of `png`.
#[pyfunction]
#[pyo3(signature = (png, out, size, fit="contain", pad_colour=None, align="centre"))]
fn fit_png(
    py: Python<'_>,
    png: PathBuf,
    out: PathBuf,
    size: (i64, i64),
    fit: &str,
    pad_colour: Option<(u8, u8, u8, u8)>,
    align: &str,
) -> PyResult<()> {
    let spec = size_spec(size, fit, pad_colour, align)?;
    guarded(py, || crate::fit_png(&png, &out, &spec))
}

/// The default pad colour (r, g, b, a) for the PNG file name `name`, by its texture type.
#[pyfunction]
fn default_pad_colour(name: PathBuf) -> PyResult<(u8, u8, u8, u8)> {
    let c = crate::default_pad_colour(&name).map_err(to_py)?;
    Ok((c[0], c[1], c[2], c[3]))
}

/// Test hook: panics inside the same guard `encode` uses, to prove a panic cannot escape.
#[pyfunction]
fn _selftest_panic(py: Python<'_>) -> PyResult<()> {
    guarded(py, || -> Result<(), Error> { panic!("selftest panic") })
}

/// Module entry point: registers the functions and the exception above under `_paa`.
/// `_selftest_panic` is exported only so the Python tests can check panic handling.
#[pymodule]
fn _paa(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(supported, m)?)?;
    m.add_function(wrap_pyfunction!(encode, m)?)?;
    m.add_function(wrap_pyfunction!(fit_png, m)?)?;
    m.add_function(wrap_pyfunction!(default_pad_colour, m)?)?;
    m.add_function(wrap_pyfunction!(_selftest_panic, m)?)?;
    m.add("NeedsImageToPAA", m.py().get_type::<NeedsImageToPAA>())?;
    m.add("MipTooLarge", m.py().get_type::<MipTooLarge>())?;
    Ok(())
}
