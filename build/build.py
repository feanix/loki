"""Build dist/Loki.exe and dist/Loki-cli.exe. Usage: python build/build.py   (needs: pip install pyinstaller pillow)

After a build it lists the exe contents and fails if any Bohemia / Arma 3 Tools file is inside.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r"ImageToPAA|TexConvert|Bohemia|LibCommon|licenseMgr", re.IGNORECASE)


def exe_contents(exe: Path) -> list[str]:
    """Names in the exe's embedded archive (PyInstaller's own reader)."""
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(exe))
    return list(reader.toc)


def forbidden_in(names: list[str]) -> list[str]:
    return [n for n in names if FORBIDDEN.search(n)]


def main() -> int:
    import build_native

    try:
        print(f"native module: {build_native.build()}", flush=True)
    except build_native.NativeBuildError as e:
        print(f"FAIL: native build: {e}")
        return 1
    cmd = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "build" / "loki.spec"),
        "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "_work"),
    ]  # fmt: skip
    print(" ".join(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc:
        return rc
    for name in ("Loki.exe", "Loki-cli.exe"):
        exe = ROOT / "dist" / name
        bad = forbidden_in(exe_contents(exe))
        if bad:
            print(f"FAIL: Bohemia files in {name}:", bad)
            return 1
        print(f"ok: {exe} ({exe.stat().st_size} bytes), no Bohemia files inside")
    return 0


if __name__ == "__main__":
    sys.exit(main())
