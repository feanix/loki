"""Build the release zip. Usage: python build/release.py [--reuse]

Builds dist/Loki.exe and dist/Loki-cli.exe (build/build.py), then writes
dist/Loki-<version>-win64.zip with Loki.exe, Loki-cli.exe, README.md, LICENSE and THIRD_PARTY_NOTICES,
and dist/SHA256SUMS (sha256sum format, checks with `sha256sum -c` or Get-FileHash). The version is
loki.__version__. `--reuse` skips the build when both exes are newer than every source file; it
fails (never silently rebuilds or ships stale exes) when they are not.
Before and after zipping it scans every decompressed exe entry (PYZ modules too) and every zip member for the
builder's user name, home folder and checkout path (build/path_scan.py); any hit fails the release, no zip is kept.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
sys.path.insert(0, str(ROOT / "src"))

from loki import __version__

DIST = ROOT / "dist"
EXES = ("Loki.exe", "Loki-cli.exe")
DOCS = ("README.md", "LICENSE", "THIRD_PARTY_NOTICES")
SOURCE_DIRS = ("src", "native/paa/src", "assets")
SOURCE_FILES = ("build/loki.spec", "build/build_native.py","build/entry.py", "build/cli_entry.py", "native/paa/Cargo.toml",
                "native/paa/Cargo.lock", "pyproject.toml")


def zip_name(version: str = __version__) -> str:
    return f"Loki-{version}-win64.zip"


def newest_source() -> float:
    """Latest mtime of anything the exes are built from (compiled output and caches excluded)."""
    files = [ROOT / f for f in SOURCE_FILES]
    for d in SOURCE_DIRS:
        files += [p for p in (ROOT / d).rglob("*") if p.is_file() and p.suffix not in (".pyc", ".pyd")]
    return max(p.stat().st_mtime for p in files if p.exists())


def exes_fresh() -> bool:
    paths = [DIST / n for n in EXES]
    return all(p.is_file() for p in paths) and min(p.stat().st_mtime for p in paths) >= newest_source()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def leaks_in(paths: list[Path]) -> list[tuple[str, str]]:
    """(entry, kind) for each place the builder's user name, home or checkout path appears (path_scan)."""
    import build_native
    import path_scan

    ndl = path_scan.needles(ROOT, extra={"target": str(build_native.target_dir())})
    return [hit for p in paths for hit in path_scan.scan_file(p, ndl)]


def print_leaks(leaks: list[tuple[str, str]]) -> None:
    print(f"FAIL: {len(leaks)} local-path leak(s); nothing released (kind only, never the path):")
    for entry, kind in leaks:
        print(f"  {entry}: {kind}")


def write_zip(out: Path) -> None:
    """Zip the five files flat (no folder), fixed order and timestamps from the files themselves."""
    members = [DIST / n for n in EXES] + [ROOT / n for n in DOCS]
    missing = [str(m) for m in members if not m.is_file()]
    if missing:
        raise SystemExit("FAIL: missing " + ", ".join(missing))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for m in members:
            z.write(m, m.name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reuse", action="store_true", help="use dist/*.exe if newer than all sources, else fail")
    args = ap.parse_args()
    if args.reuse:
        if not exes_fresh():
            print("FAIL: --reuse but dist exes are missing or older than a source file; run without --reuse")
            return 1
        print("reusing fresh dist exes")
    else:
        import build

        rc = build.main()
        if rc:
            print("FAIL: build")
            return rc
    out = DIST / zip_name()
    out.unlink(missing_ok=True)
    (DIST / "SHA256SUMS").unlink(missing_ok=True)
    leaks = leaks_in([DIST / n for n in EXES])
    if leaks:
        print_leaks(leaks)
        return 1
    write_zip(out)
    leaks = leaks_in([out])
    if leaks:
        out.unlink(missing_ok=True)
        print_leaks(leaks)
        return 1
    print("path scan: no user name, home or checkout path in the exes or the zip")
    digest = sha256(out)
    (DIST / "SHA256SUMS").write_text(f"{digest} *{out.name}\n", encoding="utf-8", newline="\n")
    print(f"version {__version__}")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    with zipfile.ZipFile(out) as z:
        for i in z.infolist():
            print(f"  {i.filename:<22}{i.file_size:>10}")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
