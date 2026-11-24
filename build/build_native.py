"""Build the native PAA encoder and copy it to src/loki/_paa.pyd. Usage: python build/build_native.py

Runs `cargo build --release --locked --features python` on native/paa and copies the cdylib to
src/loki/_paa.pyd (gitignored). No maturin needed. tests/conftest.py calls ensure() once per
session; build/build.py calls build() before PyInstaller. A failed build raises NativeBuildError:
callers must fail, never skip.

Each checkout gets its own cargo target dir outside the repo: CARGO_TARGET_DIR if set, else
%LOCALAPPDATA%/Loki/cargo-target/<first 12 hex of sha1 of the checkout's absolute path>. A dir
shared between checkouts can serve one checkout's stale loki_paa.dll to another (#30). Build +
copy still run under an exclusive file lock and the copy is checked for the module's init symbol.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "native" / "paa"
PYD = ROOT / "src" / "loki" / "_paa.pyd"
INIT_SYMBOL = b"PyInit__paa"


class NativeBuildError(RuntimeError):
    """The native module could not be built or copied."""


def target_dir(root: Path = ROOT) -> Path:
    """This checkout's cargo target dir: $CARGO_TARGET_DIR, else cargo-target/<sha1(root)[:12]>."""
    env = os.environ.get("CARGO_TARGET_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    key = hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    return Path(base) / "Loki" / "cargo-target" / key


def sources() -> list[Path]:
    """Every file whose change must rebuild the module (crate sources, manifest, lock)."""
    files = [p for p in (CRATE / "src").rglob("*") if p.is_file()]
    files += [CRATE / "Cargo.toml", CRATE / "Cargo.lock", Path(__file__)]  # this file: its rustflags (#32)
    return files


def is_stale(pyd: Path = PYD) -> bool:
    """True when the .pyd is missing or older than any native source."""
    if not pyd.is_file():
        return True
    built = pyd.stat().st_mtime
    return any(p.stat().st_mtime > built for p in sources() if p.exists())


@contextlib.contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """Cross-process lock on `lock_path`; the OS drops it if the holder dies."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as f:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # blocks, retrying for ~10 s per call
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _cdylib_name() -> str:
    return "loki_paa.dll" if os.name == "nt" else ("libloki_paa.dylib" if sys.platform == "darwin" else "libloki_paa.so")


def remap_flags(tdir: Path, root: Path = ROOT, environ: dict[str, str] | None = None) -> list[str]:
    """rustc flags that keep the builder's paths out of the binary (#32): cargo/rustup homes, checkout and
    target dir are remapped to neutral names, and on MSVC the .pdb path in the debug directory is cut to a file name."""
    env = os.environ if environ is None else environ
    home = Path(env.get("USERPROFILE") or Path.home())
    cargo = Path(env.get("CARGO_HOME") or home / ".cargo")
    rustup = Path(env.get("RUSTUP_HOME") or home / ".rustup")
    flags: list[str] = []
    # Longest prefix first: rustc applies the last matching mapping, so list the nested ones last.
    pairs = [(cargo, "cargo"), (rustup, "rustup"), (root, "loki"), (tdir, "target")]
    for src, dst in sorted(pairs, key=lambda p: len(str(p[0]))):
        flags += ["--remap-path-prefix", f"{src}={dst}"]
    if os.name == "nt":
        flags += ["-C", "link-arg=/PDBALTPATH:%_PDB%"]
    return flags


def rustflags_env(tdir: Path, root: Path = ROOT, environ: dict[str, str] | None = None) -> dict[str, str]:
    """CARGO_ENCODED_RUSTFLAGS = the caller's own flags (CARGO_ENCODED_RUSTFLAGS, else RUSTFLAGS) + remap_flags."""
    env = os.environ if environ is None else environ
    if "CARGO_ENCODED_RUSTFLAGS" in env:
        own = [f for f in env["CARGO_ENCODED_RUSTFLAGS"].split("\x1f") if f]
    else:
        own = shlex.split(env.get("RUSTFLAGS", ""), posix=False)
    return {"CARGO_ENCODED_RUSTFLAGS": "\x1f".join(own + remap_flags(tdir, root, env))}


def build(pyd: Path = PYD) -> Path:
    """Build the crate and copy the cdylib to `pyd`. Raises NativeBuildError on any failure."""
    tdir = target_dir()
    env = dict(os.environ, CARGO_TARGET_DIR=str(tdir), **rustflags_env(tdir))
    env.pop("RUSTFLAGS", None)  # folded into CARGO_ENCODED_RUSTFLAGS above (which cargo would prefer anyway)
    cmd = ["cargo", "build", "--release", "--locked", "--features", "python", "--manifest-path", str(CRATE / "Cargo.toml")]
    with _exclusive(tdir / "loki-build.lock"):
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT, check=False)
        except FileNotFoundError as e:
            raise NativeBuildError(f"cargo not found on PATH (install Rust): {e}") from e
        if r.returncode != 0:
            raise NativeBuildError(f"{' '.join(cmd)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        dll = tdir / "release" / _cdylib_name()
        if not dll.is_file():
            raise NativeBuildError(f"cargo succeeded but {dll} is missing")
        if INIT_SYMBOL not in dll.read_bytes():
            raise NativeBuildError(f"{dll} has no {INIT_SYMBOL.decode()} (built without the python feature?)")
        tmp = pyd.with_name(pyd.name + ".new")
        shutil.copyfile(dll, tmp)
        try:
            os.replace(tmp, pyd)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise NativeBuildError(f"cannot replace {pyd} (loaded by a running process?): {e}") from e
    return pyd


def ensure(pyd: Path = PYD) -> Path:
    """Build only when the .pyd is missing or stale; a failure raises NativeBuildError."""
    if is_stale(pyd):
        build(pyd)
    return pyd


def main() -> int:
    try:
        out = build()
    except NativeBuildError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"ok: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
