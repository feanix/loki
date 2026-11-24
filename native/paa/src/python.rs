//! The CPython extension module `_paa` (feature `python`).
//!
//! Two functions: `supported(name)` says whether Loki may send a PNG file name to this encoder,
//! and `encode(png, paa, max_size)` converts one PNG to PAA. The routing rule lives in Rust so
//! Python does not duplicate it.
//!   supported(name: str) -> bool
//!   encode(png: str, paa: str, max_size: int | None) -> None
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
    "The encoder refuses this file (message = short reason, e.g. 'soft alpha'): ImageToPAA must convert it."
);

create_exception!(
    _paa,
    MipTooLarge,
    PyValueError,
    "A mip's stored size does not fit the 24-bit length field of the PAA format (an incompressible 4096x4096 DXT5)."
);

/// Map a crate error to the Python exception the caller sees: a file the encoder refuses
/// (`Unsupported`) to `NeedsImageToPAA`, `MipTooLarge` to its own `ValueError` subclass, I/O errors
/// to `OSError`, everything else to `ValueError`.
/// `NeedsImageToPAA` carries only the short reason, because the caller uses it as a routing
/// signal and not as a user-facing message.
fn to_py(e: Error) -> PyErr {
    match e {
        Error::Unsupported { reason, .. } => NeedsImageToPAA::new_err(reason),
        Error::MipTooLarge { .. } => MipTooLarge::new_err(e.to_string()),
        Error::Io { .. } => PyOSError::new_err(e.to_string()),
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
        Err(_) => Err(PyRuntimeError::new_err("internal error in the PAA encoder (panic)")),
    }
}

/// True when Loki may route this PNG file name here: no suffix, or a verified rule-table suffix.
#[pyfunction]
fn supported(name: &str) -> bool {
    crate::supported(name)
}

/// Encode `png` to `paa`, halving while the longest side exceeds `max_size` (None = no limit).
#[pyfunction]
#[pyo3(signature = (png, paa, max_size=None))]
fn encode(py: Python<'_>, png: PathBuf, paa: PathBuf, max_size: Option<i64>) -> PyResult<()> {
    // max_size arrives as i64 so negative and huge values reach us and can be reported.
    // Values below 1 are rejected because no image can be halved to fit them.
    let max = match max_size {
        None => None,
        Some(m) if m >= 1 => Some(usize::try_from(m).map_err(|_| PyValueError::new_err("max_size too large"))?),
        Some(m) => return Err(PyValueError::new_err(format!("max_size must be at least 1, got {m}"))),
    };
    guarded(py, || crate::encode_file(&png, &paa, max))
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
    m.add_function(wrap_pyfunction!(_selftest_panic, m)?)?;
    m.add("NeedsImageToPAA", m.py().get_type::<NeedsImageToPAA>())?;
    m.add("MipTooLarge", m.py().get_type::<MipTooLarge>())?;
    Ok(())
}
