"""build/check_exe.py's row check must track the app's digest text (it is a manual script, so nothing else runs it)."""

import sys
from pathlib import Path

from PIL import Image

from loki import app as app_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import check_exe


def test_check_exe_row_check_accepts_the_apps_digest_and_rejects_a_missing_row(tmp_path):
    files = []
    for name in ("a.png", "b.png", "c.png"):
        p = tmp_path / "tex" / name
        p.parent.mkdir(exist_ok=True)
        Image.new("RGBA", (64, 64), (1, 2, 3, 255)).save(p)
        files.append(p)
    app = app_mod.App(tmp_path / "settings.json", find_tool=lambda _o: Path("fake.exe"), interval=0.02, settle=0.1)
    app.add_files(files)
    digest = app.digest()
    app.close()
    assert check_exe.dropped_rows_ok(digest, files)
    without_one = "\n".join(ln for ln in digest.splitlines() if str(files[1]) not in ln)
    assert not check_exe.dropped_rows_ok(without_one, files)
    assert not check_exe.dropped_rows_ok(digest, [*files, tmp_path / "tex" / "d.png"])
