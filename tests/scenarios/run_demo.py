"""Digest command: build the demo fixture in a fresh temp dir and run scenarios headlessly.

`python tests/scenarios/run_demo.py [NAME ...]` from the repo root, no environment needed. NAME is a
scenario file stem (demo, c1_no_folder, c2_drop, c3_caps, c4_bake, c5_remove, c6_persist); default demo.
Each runs in its own fresh fixture. Uses the real ImageToPAA when installed; otherwise a fake .bat tool
(first output line says which). Goes through loki.__main__.run_scenario, the same code as
`python -m loki --scenario`. Exit 0 on success.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))
sys.path.insert(0, str(HERE))

from build_demo import build

from loki import core
from loki.__main__ import run_scenario


def run_one(name: str) -> int:
    with tempfile.TemporaryDirectory(prefix="loki-demo-") as tmp:
        out = Path(tmp)
        build(out)
        scn = out / f"{name}.scn"
        if not scn.is_file():
            print(f"no scenario named {name}")
            return 2
        try:
            core.find_imagetopaa()
            print("tool: real ImageToPAA")
        except core.ToolNotFound:
            fake = out / "fake_tool.bat"
            fake.write_text('@echo off\ncopy /Y "%~1" "%~2" >nul\n')
            scn.write_text(f"tool {fake}\n" + scn.read_text(encoding="utf-8"), encoding="utf-8")
            print("tool: FAKE (ImageToPAA not installed; copies the png as the .paa)")
        print(f"--- {name}")
        return run_scenario(scn, str(out / "settings.json"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # the digest carries cell glyphs
    names = sys.argv[1:] or ["demo"]
    return max(run_one(n) for n in names)


if __name__ == "__main__":
    sys.exit(main())
