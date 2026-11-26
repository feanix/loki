"""Files dropped on the exe icon: run_window adds them through App.add_files (the window-drop handler) and
converts nothing. The window itself is replaced by a stub, so no Tk is needed."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from loki import gui


def png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (32, 32), (1, 2, 3, 255)).save(path)
    return path


@pytest.fixture(autouse=True)
def _close_apps():
    yield
    for app in OPENED:
        app.close()
    OPENED.clear()


OPENED: list = []


def open_with(monkeypatch, tmp_path, paths):
    """Run gui.run_window with a stub window; return the App it built, still open."""
    got = {}

    class StubWindow:
        def __init__(self, app):
            got["app"] = app
            OPENED.append(app)

        def run(self):
            pass

    monkeypatch.setattr(gui, "MainWindow", StubWindow)
    gui.run_window(str(tmp_path / "s.json"), [str(p) for p in paths])
    return got["app"]


def names(app) -> list[str]:
    return sorted(Path(f.key).name for f in app.settings.files())


def test_one_png_is_listed_and_not_converted(monkeypatch, tmp_path):
    a = png(tmp_path / "a_co.png")
    app = open_with(monkeypatch, tmp_path, [a])
    assert names(app) == ["a_co.png"] and app.message == "Added 1 file"
    assert not a.with_suffix(".paa").exists()


def test_several_pngs_are_listed(monkeypatch, tmp_path):
    files = [png(tmp_path / n) for n in ("a_co.png", "b_co.png", "sub/c_co.png")]
    app = open_with(monkeypatch, tmp_path, files)
    assert names(app) == ["a_co.png", "b_co.png", "c_co.png"] and app.message == "Added 3 files"


def test_a_folder_is_treated_as_a_window_drop_treats_it(monkeypatch, tmp_path):
    png(tmp_path / "tex" / "a_co.png")
    app = open_with(monkeypatch, tmp_path, [tmp_path / "tex"])
    assert names(app) == [] and app.message == "Added 0 files; skipped 1 not PNG, TGA or JPG"


def test_a_non_png_is_ignored_with_the_drop_note(monkeypatch, tmp_path):
    a = png(tmp_path / "a_co.png")
    txt = tmp_path / "notes.txt"
    txt.write_text("x")
    app = open_with(monkeypatch, tmp_path, [a, txt])
    assert names(app) == ["a_co.png"] and app.message == "Added 1 file; skipped 1 not PNG, TGA or JPG"


def test_user_guide_documents_the_icon_drop_and_the_second_window():
    """The user guide tells people they can drop PNGs on the exe icon and that a second Loki opens a second
    window, so the behaviour above is not a surprise."""
    guide = (Path(__file__).resolve().parents[1] / "docs" / "USER-GUIDE.md").read_text(encoding="utf-8")
    line = next(ln for ln in guide.splitlines() if "`Loki.exe` icon" in ln)
    assert "same as a drop on the window" in line
    assert "converts nothing" in line
    assert "second window" in line
