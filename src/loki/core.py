"""Loki core: decide the status of one PNG and convert it to PAA, with the built-in Loki encoder (_paa)
or Bohemia's ImageToPAA (decision 17).

Pure functions only: no threads, no global state, no settings reads (see docs/design.md, core.py).
Rules enforced here:
  * a PNG whose sides are not powers of two is flagged, never converted or resized;
  * an optional max-size cap halves oversized sides (aspect kept) into a temp file, the source is untouched;
  * a .paa whose top mip differs from what the file's own max size wants counts as stale;
  * a listed file that is gone is "missing" (it can come back), never an error;
  * the .paa is written into a private temp folder beside the target and moved into place with os.replace,
    so a failed run never leaves a half file or destroys a good old .paa. Every temp file keeps the
    original file name because both converters pick the format (_ca DXT5, _co DXT1) from the output name suffix;
  * `route` decides which converter handles a file; the temp/replace/lock path is shared by both.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from loki import _paa

MAX_SIZES = (2048, 1024, 512, 256)

UP_TO_DATE = "up_to_date"
NEEDS_CONVERTING = "needs_converting"
FLAGGED = "flagged"
MISSING = "missing"
PAA_IN_USE = "PAA in use"  # ConvertResult.error and row reason when the .paa target is held open (#028)
IN_USE = "in_use"  # the png is locked/open elsewhere right now: transient, never a verdict on the file

# Converter choices (settings) and route results. The suffix rule lives in the Rust lib, not here.
LOKI = "loki"
BOHEMIA = "bohemia"
CONVERTERS = (LOKI, BOHEMIA)
NEEDS_TOOL = "needs_tool"  # route result, also the row status of a needs-converting file only ImageToPAA can do
LOKI_MAX_SIDE = 4096  # the encoder refuses larger PNGs at load (#020, decision 28)

_LOCK_WINERRORS = (32, 33)  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION

_TOOL_REL = Path("steamapps") / "common" / "Arma 3 Tools" / "ImageToPAA" / "ImageToPAA.exe"
_TOOL_TIMEOUT = 300  # seconds; big textures take a few seconds, a hung tool must not hang the GUI


CONVERT_WORKERS_MAX = 16
CONVERT_CPU_RESERVE = 4  # logical CPUs left for the GUI, the watcher and the OS


def convert_workers(cpu_count: int | None = None) -> int:
    """How many ImageToPAA processes may run at once (decision 13): the one definition of the limit."""
    cpu = os.cpu_count() if cpu_count is None else cpu_count
    return min(CONVERT_WORKERS_MAX, max(1, (cpu or 1) - CONVERT_CPU_RESERVE))


def file_key(path: str | Path) -> str:
    """The one identity rule for a listed file: absolute and case/slash-normalised (Windows).
    Every module compares files through this, none re-derives it."""
    return os.path.normcase(os.path.abspath(path))


@dataclass(frozen=True)
class PngEntry:
    path: Path
    status: str
    reason: str | None
    size: tuple[int, int] | None
    detail: str | None = None  # full technical text behind `reason` (for the GUI status bar)


@dataclass(frozen=True)
class ConvertResult:
    png: Path
    paa: Path
    ok: bool
    error: str | None
    retry: bool = False  # failed only because the png or its .paa was locked: worth trying again later
    converter: str | None = None  # LOKI or BOHEMIA: who wrote the .paa (None when nothing was written)
    needs_tool: bool = False  # not converted: only ImageToPAA can do this file and it is absent (error holds the row text)


def is_lock_error(exc: BaseException) -> bool:
    """The one test for "a file is locked by someone else right now" (sharing/lock violation)."""
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:  # Windows: only the sharing/lock codes; winerror 5 (access denied) is not a lock
        return isinstance(exc, OSError) and winerror in _LOCK_WINERRORS
    return isinstance(exc, PermissionError)  # no winerror (non-Windows): EACCES/EPERM, kept as lock


def _acl_denied(exc: BaseException, path: Path) -> bool:
    """True when a winerror-less PermissionError on Windows is really access denied (winerror 5).

    Python's open() reports sharing violations and ACL denials alike as errno 13 with no winerror, so
    is_lock_error alone cannot tell them apart; a CreateFile probe with every share mode allowed can.
    """
    if sys.platform != "win32" or not isinstance(exc, PermissionError) or getattr(exc, "winerror", None) is not None:
        return False
    return _probe_error(path, 0x80000000) == 5  # GENERIC_READ


def _probe_error(path: Path, access: int) -> int | None:
    """Open `path` with `access` and every share mode allowed; the Win32 error on failure, None on success."""
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    # share read|write|delete, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
    handle = k32.CreateFileW(str(path), access, 7, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        return ctypes.get_last_error()
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle(handle)
    return None


def _held_open(exc: BaseException, path: Path) -> bool:
    """True when a failed replace of `path` (winerror 5) is really another process holding the file open (#028).

    The same probe as _acl_denied, asking for DELETE access: a hold answers sharing violation (32), an ACL denial 5.
    """
    if sys.platform != "win32" or not isinstance(exc, PermissionError):
        return False
    return _probe_error(path, 0x00010000) in _LOCK_WINERRORS  # DELETE


class ToolNotFound(Exception):
    """ImageToPAA.exe could not be located; the message is fit to show to the user as is."""


def _is_pow2(n: int) -> bool:
    return n > 0 and n & (n - 1) == 0


def _nearest_pow2_hint(width: int, height: int) -> str:
    def nearest(n: int) -> int:
        lo = 1 << (n.bit_length() - 1)
        return lo if n - lo < 2 * lo - n else 2 * lo  # ties go up

    return f"{nearest(width)}x{nearest(height)}"


def _sibling_paa(png: Path) -> Path | None:
    """Existing .paa with the same stem (any case), or None."""
    want = png.stem.lower() + ".paa"
    try:
        for p in png.parent.iterdir():
            if p.name.lower() == want and p.is_file():
                return p
    except OSError:
        return None
    return None


_PAA_HEAD_BYTES = 4096  # tag + TAGG blocks (~100 bytes from ImageToPAA) + first mip size; a bounded read
_MAX_TAGGS = 32


def paa_size(path: str | Path) -> tuple[int, int] | None:
    """Top-mip (width, height) from a .paa header, or None when unreadable. Reads at most 4 KiB."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_PAA_HEAD_BYTES)
    except OSError:
        return None
    n = len(data)
    if n < 2:
        return None
    pos = 2  # u16 type tag
    for _ in range(_MAX_TAGGS):
        if data[pos : pos + 4] != b"GGAT":
            break
        if pos + 12 > n:
            return None
        (length,) = struct.unpack_from("<I", data, pos + 8)
        pos += 12 + length
        if pos > n:
            return None
    else:
        return None
    if pos + 2 > n:
        return None
    (palette,) = struct.unpack_from("<H", data, pos)
    pos += 2 + 3 * palette
    if pos + 4 > n:  # a palette count running past the bounded read means unreadable
        return None
    width, height = struct.unpack_from("<HH", data, pos)
    width &= 0x7FFF  # high bit marks LZO-compressed mip data
    if width == 0 or height == 0:
        return None
    return width, height


def check_png(path: str | Path, max_size: int | None = None) -> PngEntry:
    """Status of one png. Missing and flagged win; a missing/older/unreadable/wrong-size .paa needs converting."""
    path = Path(path).absolute()
    try:
        with Image.open(path) as im:  # header only, no full decode
            size = im.size
    except FileNotFoundError:
        return PngEntry(path, MISSING, None, None)
    except Exception as exc:  # noqa: BLE001 -- Pillow raises several types for corrupt files
        detail = f"{type(exc).__name__}: {exc}"
        if is_lock_error(exc) and not _acl_denied(exc, path):  # transient: the owner will release it, so it is waiting, not flagged
            return PngEntry(path, IN_USE, "file in use", None, detail)
        # Short, path-free reason for the row; the raw exception stays in `detail`.
        if isinstance(exc, UnidentifiedImageError) or not isinstance(exc, OSError):
            why = "not a valid PNG"
        elif isinstance(exc, PermissionError):  # here only winerror 5 and the like: not a lock
            why = "access denied"
        else:
            why = "unreadable"
        return PngEntry(path, FLAGGED, f"cannot read: {why}", None, detail)
    w, h = size
    if not (_is_pow2(w) and _is_pow2(h)):
        reason = f"{w}x{h}: sides must be powers of two (e.g. {_nearest_pow2_hint(w, h)})"
        return PngEntry(path, FLAGGED, reason, size)
    paa = _sibling_paa(path)
    try:
        fresh = paa is not None and paa.stat().st_mtime >= path.stat().st_mtime
    except OSError:
        fresh = False
    if not fresh:
        return PngEntry(path, NEEDS_CONVERTING, None, size)
    have = paa_size(paa)
    if have is None:
        return PngEntry(path, NEEDS_CONVERTING, "PAA unreadable", size)
    want = target_size(w, h, max_size)
    if have != want:
        reason = f"PAA is {have[0]}x{have[1]}, max size wants {want[0]}x{want[1]}"
        return PngEntry(path, NEEDS_CONVERTING, reason, size)
    return PngEntry(path, UP_TO_DATE, None, size)


# ImageToPAA's output limit as measured (#029): 8192x8192, 8192x2048, 2048x8192 and 8192x4096 PNGs come out
# as 4096x4096, 4096x1024, 1024x4096 and 4096x2048 PAAs (halved, aspect kept, until both sides <= 4096).
IMAGETOPAA_MAX_SIDE = 4096


def target_size(width: int, height: int, max_size: int | None) -> tuple[int, int]:
    """While either side is over the cap, halve both (aspect kept); a side never drops below 1.
    Max size off still caps at IMAGETOPAA_MAX_SIDE; a lower cap wins."""
    max_size = IMAGETOPAA_MAX_SIDE if max_size is None else min(max_size, IMAGETOPAA_MAX_SIDE)
    while max(width, height) > max_size:
        width, height = max(width // 2, 1), max(height // 2, 1)
    return width, height


# --- ImageToPAA discovery ---------------------------------------------------------------------------

def _read_registry_steam_paths() -> list[Path]:
    """Steam install dirs from the registry; empty off Windows or when the keys are absent."""
    try:
        import winreg
    except ImportError:
        return []
    found: list[Path] = []
    for hive, key, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key) as k:
                found.append(Path(winreg.QueryValueEx(k, value)[0]))
        except OSError:
            continue
    return found


def _vdf_library_paths(vdf: Path) -> list[Path]:
    """Every "path" value in libraryfolders.vdf. Minimal parse; malformed or missing -> []."""
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
        out.append(Path(m.group(1).replace("\\\\", "\\")))
    return out


def find_imagetopaa(
    override: str | Path | None = None,
    steam_roots: Callable[[], Iterable[str | Path]] | None = None,
) -> Path:
    """Locate ImageToPAA.exe. `steam_roots` replaces the registry read (tests use a fake layout)."""
    if override:
        p = Path(override)
        if p.is_file():
            return p
        raise ToolNotFound(f"ImageToPAA not found at the configured path: {p}")
    roots = list((steam_roots or _read_registry_steam_paths)())
    tried: list[Path] = []
    seen: set[str] = set()
    libraries: list[Path] = []
    for root in roots:
        root = Path(root)
        for lib in [root, *_vdf_library_paths(root / "steamapps" / "libraryfolders.vdf")]:
            key = os.path.normcase(str(lib))
            if key not in seen:
                seen.add(key)
                libraries.append(lib)
    for lib in libraries:
        cand = lib / _TOOL_REL
        tried.append(cand)
        if cand.is_file():
            return cand
    if not roots:
        raise ToolNotFound(
            "ImageToPAA not found: Steam was not detected. Install Arma 3 Tools from Steam "
            "or set the ImageToPAA path manually."
        )
    raise ToolNotFound(
        "ImageToPAA not found in any Steam library (looked for "
        f"{_TOOL_REL.as_posix()}). Install Arma 3 Tools from Steam or set the path manually."
    )


# --- conversion -------------------------------------------------------------------------------------

def _run_tool(tool: Path, src: Path, dst: Path) -> str | None:
    """Run ImageToPAA; return an error string or None on success."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            [str(tool), str(src), str(dst)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_TOOL_TIMEOUT,
            check=False,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        return f"ImageToPAA timed out after {_TOOL_TIMEOUT}s"
    except OSError as exc:
        return f"cannot run ImageToPAA: {exc}"
    if proc.returncode != 0:
        detail = (proc.stdout.strip() + " " + proc.stderr.strip()).strip()
        return f"ImageToPAA failed (exit {proc.returncode}): {detail}".rstrip(": ")
    if not dst.is_file() or dst.stat().st_size == 0:
        return "ImageToPAA reported success but wrote no output"
    return None


def _run_loki(src: Path, dst: Path, max_size: int | None) -> str | None:
    """Encode in-process with the native encoder (GIL released, so pool workers run in parallel).
    Bad input comes back as text; an OSError is left to convert()'s handler so a lock is still seen as a lock.
    A per-file refusal (`_paa.NeedsImageToPAA`) is not caught here: convert() falls back to ImageToPAA."""
    try:
        _paa.encode(str(src), str(dst), max_size)
    except _paa.NeedsImageToPAA:
        raise
    except _paa.MipTooLarge:  # no hand-over: ImageToPAA wraps the 24-bit length field and writes a corrupt file
        return MIP_TOO_LARGE
    except (ValueError, RuntimeError) as exc:
        return f"Loki converter: {exc}"
    return None


MIP_TOO_LARGE = "too detailed to fit at 4096 as DXT5: set max size 2048"


def _png_depth_type(png: Path) -> tuple[int, int] | None:
    """(bit depth, colour type) from the PNG header, None when the file does not start like a PNG."""
    with open(png, "rb") as f:
        head = f.read(26)
    if len(head) < 26 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return head[24], head[25]


def _to_8bit_rgb(im: Image.Image, depth: int) -> Image.Image:
    """8-bit RGB, or RGBA when the image has alpha or a tRNS key. Never a clipping conversion:
    16-bit grey keeps its high byte (0x80FF -> 0x80), not Pillow's clip to 255."""
    if im.mode in ("I;16", "I"):  # 16-bit grey; Pillow keeps the full 16 bits in these modes
        raw = im.tobytes()
        high = raw[1::2] if im.mode == "I;16" else raw[1::4]  # little endian: byte 1 is the high byte
        key = im.info.get("transparency")
        im = Image.frombytes("L", im.size, high)
        if isinstance(key, int) and key <= 0xFFFF:
            im.info["transparency"] = key >> 8
    elif depth == 16 and isinstance(im.info.get("transparency"), tuple):
        im.info["transparency"] = tuple(v >> 8 for v in im.info["transparency"])  # key is 16-bit, pixels now 8-bit
    return im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")


def _imagetopaa_input(png: Path, tdir: Path, max_size: int | None) -> Path:
    """The file to hand to ImageToPAA: the PNG itself when it is 8-bit RGB/RGBA (and needs no resize), else
    a copy in `tdir` that is 8-bit RGB/RGBA (ImageToPAA mangles grey, 16-bit and palette PNGs, and ignores tRNS
    colour keys) and halved to fit `max_size`. The source PNG is never touched; `tdir` is the caller's temp dir."""
    kind = _png_depth_type(png)
    with Image.open(png) as im:
        w, h = im.size
        new = target_size(w, h, max_size) if max_size is not None else (w, h)
        plain = kind is not None and kind[0] == 8 and kind[1] in (2, 6) and "transparency" not in im.info
        if plain and new == (w, h):
            return png
        im.load()
        im = _to_8bit_rgb(im, kind[0] if kind else 8)
        if new != (w, h):
            im = im.resize(new, Image.Resampling.LANCZOS)
        dest = tdir / png.name
        im.save(dest, format="PNG")
        return dest


def suffix_of(png: str | Path) -> str:
    """The texture-type suffix of a file name ("_nohq" for rock_nohq.png), "" when there is none."""
    stem = Path(png).stem
    i = stem.rfind("_")
    return stem[i:].lower() if i > 0 else ""


def route(png: str | Path, size: tuple[int, int] | None, converter: str, tool_found: bool) -> str:
    """Which converter handles this file: LOKI, BOHEMIA or NEEDS_TOOL (ImageToPAA needed but absent).

    Loki's encoder takes what the Rust lib says it supports (`_paa.supported`) up to 4096 per side
    (`size` is the PNG header size); everything else goes to ImageToPAA when it is there."""
    if converter == LOKI and _paa.supported(Path(png).name) and (size is None or max(size) <= LOKI_MAX_SIDE):
        return LOKI
    return BOHEMIA if tool_found else NEEDS_TOOL


def needs_tool_reason(png: str | Path, size: tuple[int, int] | None, converter: str = LOKI) -> str:
    """Row text for a file that route() sent to NEEDS_TOOL."""
    if converter != LOKI:
        return "ImageToPAA.exe not found"
    if not _paa.supported(Path(png).name):
        return f"needs Arma 3 Tools for {suffix_of(png)}"
    return f"over {LOKI_MAX_SIDE}: needs Arma 3 Tools"


_tmp_counter = itertools.count()
_REPLACE_TRIES = 10
_REPLACE_PAUSE = 0.05  # s between tries; ~0.5 s in all


def _replace_retrying(src: Path, dst: Path) -> None:
    """os.replace that rides out a reader briefly holding the old target.

    On Windows, replacing a file another handle has open without delete sharing (our own watcher or a
    virus scanner reading the previous .paa) fails with winerror 5 (access denied), not a sharing
    violation, and the hold lasts milliseconds. A real ACL denial still fails once the tries run out.
    """
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if attempt == _REPLACE_TRIES - 1:
                raise
            if getattr(exc, "winerror", None) not in (None, 5) and not is_lock_error(exc):
                raise
            time.sleep(_REPLACE_PAUSE)


def convert(
    png: str | Path, tool: str | Path | None, max_size: int | None = None, converter: str = BOHEMIA
) -> ConvertResult:
    """Convert png -> sibling .paa (overwriting). Failures come back in .error, never raised.

    `converter` is the user's choice; `route` may still send a file to ImageToPAA (`tool`, None when absent)."""
    png = Path(png)
    paa = png.with_suffix(".paa")
    tool = Path(tool) if tool is not None else None
    # Same volume as the target so os.replace is atomic; the file name inside stays the original.
    out_dir = paa.parent / f".loki-tmp-{os.getpid()}-{next(_tmp_counter)}"
    try:
        out_dir.mkdir()
        with open(png, "rb"):  # probe: a lock surfaces here as a lock, not as an opaque tool failure
            pass
        size = None
        if converter == LOKI:
            with Image.open(png) as im:  # header only: routing needs the size, as check_png reads it
                size = im.size
        used = route(png, size, converter, tool is not None)
        if used == NEEDS_TOOL:
            return ConvertResult(png, paa, False, needs_tool_reason(png, size, converter))
        with tempfile.TemporaryDirectory(prefix="loki-") as tdir:
            tmp_paa = out_dir / paa.name
            if used == LOKI:
                try:
                    err = _run_loki(png, tmp_paa, max_size)
                except _paa.NeedsImageToPAA as refusal:  # the encoder decides per file; Python never predicts it
                    if tool is None:
                        return ConvertResult(png, paa, False, f"needs Arma 3 Tools ({refusal})", needs_tool=True)
                    used = BOHEMIA
            if used == BOHEMIA:
                src = _imagetopaa_input(png, Path(tdir), max_size)
                err = _run_tool(tool, src, tmp_paa)
            if err is None:
                try:
                    _replace_retrying(tmp_paa, paa)
                except PermissionError as exc:
                    if _held_open(exc, paa):  # another process has the old .paa open: wait, keep it intact
                        return ConvertResult(png, paa, False, PAA_IN_USE, retry=True)
                    raise
        if err is not None:
            return ConvertResult(png, paa, False, err)
        return ConvertResult(png, paa, True, None, converter=used)
    except Exception as exc:  # noqa: BLE001 -- contract: never raise for file or tool failures
        return ConvertResult(png, paa, False, f"{type(exc).__name__}: {exc}", retry=is_lock_error(exc) and not _acl_denied(exc, png))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def file_sig(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) of a file, or None when it cannot be read: the shared change signature."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)
