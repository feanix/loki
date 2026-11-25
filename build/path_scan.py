"""Find the builder's local paths inside shipped binaries (release blocker).

The public zip must not carry the builder's user name, home folder or checkout path. Needles are derived at run
time (getpass user, USERPROFILE/home, checkout root; also the cargo target dir), matched case-insensitively, in
both slash directions, as UTF-8/ASCII and UTF-16LE. Findings name the entry and the kind only, never the match.

Usage: python build/path_scan.py FILE_OR_ZIP...   (an exe is read entry by entry, PYZ modules included)
"""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_PLAIN = 4  # a shorter bare user name would match everything; it is then only matched inside a path


def _variants(text: str) -> set[str]:
    return {text, text.replace("\\", "/"), text.replace("/", "\\")}


def needles(
    root: Path = ROOT, user: str | None = None, home: str | None = None, extra: dict[str, str] | None = None
) -> dict[str, list[bytes]]:
    """Kind -> lowercase byte needles. `extra` adds kinds (e.g. the cargo target dir) or overrides for tests."""
    user = getpass.getuser() if user is None else user
    home = home if home is not None else (os.environ.get("USERPROFILE") or str(Path.home()))
    texts: dict[str, set[str]] = {}
    if user:
        if len(user) >= MIN_PLAIN:
            texts["user"] = {user}
        else:
            texts["user"] = {f"{s}{user}{s}" for s in "\\/"}
    if home:
        texts["home"] = _variants(str(home).rstrip("\\/"))
    texts["checkout"] = _variants(str(root).rstrip("\\/"))
    for kind, value in (extra or {}).items():
        if value:
            texts[kind] = _variants(value.rstrip("\\/"))
    out: dict[str, list[bytes]] = {}
    for kind, vs in texts.items():
        bs: set[bytes] = set()
        for v in vs:
            v = v.lower()
            bs.add(v.encode("utf-8"))
            bs.add(v.encode("utf-16-le"))
        out[kind] = sorted(bs)
    return out


def scan_bytes(data: bytes, ndl: dict[str, list[bytes]]) -> list[str]:
    """Kinds whose needle occurs in `data` (case-insensitive)."""
    low = data.lower()
    return [k for k, bs in ndl.items() if any(b in low for b in bs)]


def count_bytes(data: bytes, ndl: dict[str, list[bytes]]) -> dict[str, int]:
    """Occurrences per kind (for before/after reports)."""
    low = data.lower()
    return {k: sum(low.count(b) for b in bs) for k, bs in ndl.items()}


def exe_entries(exe: Path):
    """Yield (name, bytes) for the exe's raw bytes, every CArchive entry and every PYZ module, decompressed."""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    yield "<raw exe>", exe.read_bytes()
    reader = CArchiveReader(str(exe))
    for name in reader.toc:
        data = reader.extract(name)
        yield name, data
        if name.upper().endswith(".PYZ") or name == "PYZ-00.pyz":
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "x.pyz"
                p.write_bytes(data)
                z = ZlibArchiveReader(str(p))
                for mod in z.toc:
                    raw = z.extract(mod, raw=True)  # marshalled code: co_filename strings are in the bytes
                    if raw:
                        yield f"{name}:{mod}", raw


def scan_exe(exe: Path, ndl: dict[str, list[bytes]]) -> list[tuple[str, str]]:
    """(entry, kind) for every hit inside the exe."""
    return [(f"{exe.name}/{n}", k) for n, d in exe_entries(exe) for k in scan_bytes(d, ndl)]


def scan_zip(zpath: Path, ndl: dict[str, list[bytes]]) -> list[tuple[str, str]]:
    """(entry, kind) for every hit in any zip member; .exe members are read entry by entry."""
    hits: list[tuple[str, str]] = []
    with zipfile.ZipFile(zpath) as z, tempfile.TemporaryDirectory() as td:
        for info in z.infolist():
            data = z.read(info)
            hits += [(f"{zpath.name}/{info.filename}", k) for k in scan_bytes(data, ndl)]
            if info.filename.lower().endswith(".exe"):
                p = Path(td) / Path(info.filename).name
                p.write_bytes(data)
                hits += [(f"{zpath.name}/{n}", k) for n, k in scan_exe(p, ndl)]
    return hits


def scan_file(path: Path, ndl: dict[str, list[bytes]]) -> list[tuple[str, str]]:
    if path.suffix.lower() == ".zip":
        return scan_zip(path, ndl)
    if path.suffix.lower() == ".exe":
        return scan_exe(path, ndl)
    return [(path.name, k) for k in scan_bytes(path.read_bytes(), ndl)]


def main(argv: list[str]) -> int:
    ndl = needles(extra=_target_extra())
    bad = 0
    for a in argv:
        p = Path(a)
        hits = scan_file(p, ndl)
        counts = count_bytes(p.read_bytes(), ndl) if p.suffix.lower() != ".zip" else {}
        print(f"{p}: {len(hits)} hits; raw counts {counts}")
        for entry, kind in hits:
            print(f"  LEAK {entry}: {kind}")
        bad += len(hits)
    return 1 if bad else 0


def _target_extra() -> dict[str, str]:
    sys.path.insert(0, str(ROOT / "build"))
    import build_native

    return {"target": str(build_native.target_dir())}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
