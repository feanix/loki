"""Build the demo fixture for the scenarios in tests/scenarios/*.scn.

Usage: python tests/scenarios/build_demo.py OUTDIR
Creates OUTDIR/demo_data (a few texture folders: good, non power-of-two, corrupt, one already
converted, plus a text file and an empty `away` folder) and copies every *.scn next to it, so
`python -m loki --scenario OUTDIR/demo.scn --settings OUTDIR/settings.json` runs without touching the
repo or the real settings file. Scenario paths are relative to the scenario file.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paa_helpers import paa_bytes  # a header-correct PAA for the already converted file


def build(outdir: Path) -> Path:
    data = outdir / "demo_data"
    (data / "away").mkdir(parents=True, exist_ok=True)
    for rel, size in (
        ("textures/stone_co.png", (256, 256)),
        ("textures/grass_co.png", (512, 256)),  # same size as rock: two caps, two PAA sizes
        ("textures/rock_co.png", (512, 256)),
        ("textures/old_co.png", (128, 128)),
        ("weapons/rifle_co.png", (256, 256)),
        ("weapons/odd_co.png", (300, 200)),  # flagged: not powers of two
    ):
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", size, (90, 120, 60, 255)).save(data / rel)
    (data / "weapons" / "broken_co.png").write_bytes(b"not a png")  # flagged: cannot read
    (data / "notes.txt").write_text("not a texture", encoding="utf-8")  # a drop must ignore it
    time.sleep(0.05)
    (data / "textures" / "old_co.paa").write_bytes(paa_bytes(128, 128))  # newer than its png: up to date
    for scn in Path(__file__).parent.glob("*.scn"):
        shutil.copy(scn, outdir / scn.name)
    return data


if __name__ == "__main__":
    print(build(Path(sys.argv[1]).resolve()))
