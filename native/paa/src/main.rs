//! paa-proto: a small command line tool for the loki_paa texture library.
//!
//! This is a developer tool for working on the encoder. It is not part of the Loki download:
//! Loki itself uses the library through the Python module in `python.rs`. Reach for paa-proto
//! when you want to encode one texture by hand, look inside a .paa file, turn a mip back into
//! a PNG to compare it by eye, or read the suffix rule table.
//!
//! Subcommands:
//!   encode <in.png> <out.paa> [--max-size N]
//!   decode <in.paa> <out.png> [--mip K]
//!   info   <in.paa>
//!   rules
//!   --help
//!
//! Examples:
//!   paa-proto encode rock_co.png rock_co.paa
//!   paa-proto encode sky_ca.png sky_ca.paa --max-size 1024
//!   paa-proto info rock_co.paa
//!   paa-proto decode rock_co.paa rock_mip1.png --mip 1
//!   paa-proto rules
//!
//! The texture format is chosen from the input file name suffix (`_co`, `_ca`, `_nohq`, ...) by
//! the rule table, which `paa-proto rules` prints. Suffixes with noise mip filters (`_cat`,
//! `_can`, `_non`) are encoded with Loki's own seeded noise (`noise.rs`), matched on measured
//! noise strength, grain and alpha coverage to ImageToPAA's, not byte for byte.
//!
//! Results go to stdout; errors go to stderr as `error: <message>`.
//!
//! Exit codes: 0 ok, 1 the operation failed (bad file, unsupported input, I/O error),
//! 2 command line misuse (unknown subcommand, missing or extra arguments, bad option value).

use loki_paa::{mip_raw, Error, Image};
use std::borrow::Cow;
use std::ffi::OsString;
use std::path::Path;
use std::process::ExitCode;

/// The help text, printed by `--help` and attached to every command line misuse error.
const USAGE: &str = "usage:\n  paa-proto encode <in.png> <out.paa> [--max-size N]\n  paa-proto decode <in.paa> <out.png> [--mip K]\n  paa-proto info <in.paa>\n  paa-proto rules\n  paa-proto --help";

/// A usage error carrying the help text; `main` exits 2 on it.
fn usage() -> Error {
    Error::Usage(USAGE.into())
}

/// Remove `--name value` from `args` and return the number, or None if the option is absent.
/// A missing or non-numeric value is a usage error. Taking options out first leaves only the
/// positional arguments for the caller to count.
fn take_opt(args: &mut Vec<String>, name: &str) -> Result<Option<usize>, Error> {
    if let Some(i) = args.iter().position(|a| a == name) {
        let v = args
            .get(i + 1)
            .ok_or_else(|| Error::Usage(format!("{name} needs a value")))?;
        let n = v
            .parse::<usize>()
            .map_err(|e| Error::Usage(format!("{name}: {v}: {e}")))?;
        args.drain(i..i + 2);
        return Ok(Some(n));
    }
    Ok(None)
}

/// `encode <in.png> <out.paa> [--max-size N]`: encode one PNG, choosing the format from its
/// file name suffix. `--max-size N` halves the image until its longest side is at most N.
fn cmd_encode(mut args: Vec<String>) -> Result<(), Error> {
    let max_size = take_opt(&mut args, "--max-size")?;
    if args.len() != 2 {
        return Err(usage());
    }
    let (inp, outp) = (Path::new(&args[0]), Path::new(&args[1]));
    loki_paa::encode_file(inp, outp, max_size)?;
    let len = std::fs::metadata(outp)
        .map_err(|e| Error::io(outp, e))?
        .len();
    println!("{} -> {} ({len} bytes)", inp.display(), outp.display());
    Ok(())
}

/// True for the uncompressed pixel formats (ARGB1555, ARGB4444, AI88). Their mips are packed
/// with LZSS, never LZO, so `loki_paa::mip_raw` does not apply: `decode_mip` unpacks the LZSS stream
/// itself and the stored bytes are passed to it as they are.
fn is_pixel_type(ty: u16) -> bool {
    matches!(ty, 0x1555 | 0x4444 | 0x8080)
}

/// Human readable name of a PAA type word, for `info`.
fn type_name(ty: u16) -> &'static str {
    match ty {
        loki_paa::TYPE_DXT1 => "DXT1",
        loki_paa::TYPE_DXT5 => "DXT5",
        0x1555 => "ARGB1555",
        0x4444 => "ARGB4444",
        0x8080 => "AI88",
        _ => "unsupported",
    }
}

/// `decode <in.paa> <out.png> [--mip K]`: write one mip of a .paa file as an RGBA PNG.
/// `--mip K` is an index into the file's mip chain, counted from 0, which is the largest
/// (full size) level; each following index is half the size of the one before. Default 0.
fn cmd_decode(mut args: Vec<String>) -> Result<(), Error> {
    let mip = take_opt(&mut args, "--mip")?.unwrap_or(0);
    if args.len() != 2 {
        return Err(usage());
    }
    let src = Path::new(&args[0]);
    let p = loki_paa::read(&std::fs::read(src).map_err(|e| Error::io(src, e))?)?;
    let m = p
        .mips
        .get(mip)
        .ok_or_else(|| Error::Paa(format!("no mip {mip} (file has {})", p.mips.len())))?;
    let raw = if is_pixel_type(p.ty) {
        Cow::Borrowed(m.data.as_slice())
    } else {
        mip_raw(m, p.ty == loki_paa::TYPE_DXT5)?
    };
    let rgba = loki_paa::decode_mip(p.ty, &raw, m.w as usize, m.h as usize)?;
    loki_paa::save_png(
        Path::new(&args[1]),
        &Image {
            w: m.w as usize,
            h: m.h as usize,
            rgba,
        },
    )
}

/// `info <in.paa>`: print the type, the tags (as hex) and each mip's size and stored length.
/// `raw=` is the size after LZO is undone (or `lzss` for the pixel formats, which are not
/// unpacked here); a mip whose LZO data is damaged shows the error text there instead.
fn cmd_info(args: Vec<String>) -> Result<(), Error> {
    if args.len() != 1 {
        return Err(usage());
    }
    let src = Path::new(&args[0]);
    let bytes = std::fs::read(src).map_err(|e| Error::io(src, e))?;
    let p = loki_paa::read(&bytes)?;
    println!("type 0x{:04X} ({})", p.ty, type_name(p.ty));
    for t in &p.tags {
        let hex: Vec<String> = t.data.iter().map(|b| format!("{b:02x}")).collect();
        println!("tag {} {}", t.name, hex.join(" "));
    }
    let dxt5 = p.ty == loki_paa::TYPE_DXT5;
    for (i, m) in p.mips.iter().enumerate() {
        let raw = if is_pixel_type(p.ty) {
            "lzss".to_string()
        } else {
            mip_raw(m, dxt5)
                .map(|r| r.len().to_string())
                .unwrap_or_else(|e| e.to_string())
        };
        println!(
            "mip {i} {}x{} lzo={} stored={} raw={raw}",
            m.w,
            m.h,
            m.lzo,
            m.data.len()
        );
    }
    println!("file size {}", bytes.len());
    Ok(())
}

/// `rules`: print the suffix rule table (which format, filter and swizzle each file name
/// suffix gets, and whether Loki routes it to this encoder). Takes no arguments.
fn cmd_rules(args: Vec<String>) -> Result<(), Error> {
    if !args.is_empty() {
        return Err(usage());
    }
    print!("{}", loki_paa::rules::table_text());
    Ok(())
}

/// The command line as text. `std::env::args()` panics (exit 101) on an argument that is not valid
/// Unicode (a lone surrogate on Windows), so read `args_os` and refuse such an argument as a usage
/// error instead.
fn text_args(args: impl Iterator<Item = OsString>) -> Result<Vec<String>, Error> {
    args.map(|a| {
        a.into_string()
            .map_err(|a| Error::Usage(format!("argument {a:?} is not valid Unicode")))
    })
    .collect()
}

/// Dispatch on the first argument and map the result to the exit code described at the top
/// of this file: 0 success, 2 for a usage error or no arguments at all (help text on stderr),
/// 1 for any other failure. `--help` (also `-h` or `help`) prints the help text and exits 0.
fn main() -> ExitCode {
    let mut args = match text_args(std::env::args_os().skip(1)) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {e}");
            return ExitCode::from(2);
        }
    };
    if args.is_empty() {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    }
    let cmd = args.remove(0);
    let r = match cmd.as_str() {
        "--help" | "-h" | "help" => {
            println!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        "encode" => cmd_encode(args),
        "decode" => cmd_decode(args),
        "info" => cmd_info(args),
        "rules" => cmd_rules(args),
        _ => Err(usage()),
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(e @ Error::Usage(_)) => {
            eprintln!("error: {e}");
            ExitCode::from(2)
        }
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{text_args, Error, USAGE};
    use std::ffi::OsString;

    /// An argument that is not valid Unicode (a lone surrogate on Windows, a bad byte elsewhere)
    /// is a usage error (exit 2), not a panic (exit 101); valid arguments pass through unchanged.
    #[test]
    fn a_non_unicode_argument_is_a_usage_error() {
        #[cfg(windows)]
        let bad = std::os::windows::ffi::OsStringExt::from_wide(&[0x61, 0xD800, 0x62]);
        #[cfg(unix)]
        let bad = std::os::unix::ffi::OsStringExt::from_vec(vec![0x61, 0xFF, 0x62]);
        let r = text_args([OsString::from("info"), bad].into_iter());
        assert!(matches!(r, Err(Error::Usage(_))), "{r:?}");
        assert_eq!(
            text_args([OsString::from("info"), OsString::from("a b.paa")].into_iter()).unwrap(),
            ["info", "a b.paa"]
        );
    }

    /// `--help` prints USAGE, so every subcommand the dispatcher in `main` accepts must be listed.
    #[test]
    fn usage_lists_every_subcommand() {
        for cmd in ["encode", "decode", "info", "rules", "--help"] {
            assert!(
                USAGE.contains(&format!("paa-proto {cmd}")),
                "USAGE does not list {cmd}"
            );
        }
    }
}
