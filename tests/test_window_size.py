"""Output size in the window: the per-row setting in the model, the settings file, the headless
scenario commands and the dialog. The encoder and the up-to-date record are tested in tests/test_output_size.py; these
tests check the wiring: the row cell, exclusivity with max size, status, persistence, and what Convert all does."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import track_app
from loki import app as app_mod
from loki import core, gui
from loki.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def png(path: Path, size=(64, 64)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, (200, 100, 50, 255)).save(path)
    return path


@pytest.fixture(autouse=True)
def no_tool(monkeypatch):
    def absent(override=None, steam_roots=None):
        raise core.ToolNotFound("absent")

    monkeypatch.setattr(core, "find_imagetopaa", absent)  # the Loki encoder does all of these


def wait_for(app, cond, timeout=20.0):
    end = time.monotonic() + timeout
    while not cond():
        app.pump()
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)
    app.pump()


def make_app(tmp_path):
    return track_app(app_mod.App(tmp_path / "settings.json", interval=0.02, settle=0.1))


# --- scenarios (the digest the headless runner prints) ------------------------------------------------------


def run(tmp_path, script: str):
    scn = tmp_path / "run.scn"
    scn.write_text(script, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "loki", "--scenario", str(scn), "--settings", str(tmp_path / "s.json")],
        capture_output=True, text=True, encoding="utf-8", timeout=120, env=env, cwd=tmp_path, check=False,
    )  # fmt: skip


def blocks(out: str) -> list[list[str]]:
    return [b.splitlines() for b in out.strip().split("\n\n")]


def row(block: list[str], name: str) -> str:
    (line,) = [ln for ln in block if (os.sep + name + " | ") in ln]
    return line.strip().split(os.sep)[-1]


def test_scenario_set_convert_and_read_the_paa_dimensions_back(tmp_path):
    p = png(tmp_path / "tex" / "door_nohq.png", (64, 64))
    r = run(tmp_path, f"converter loki\ndrop {p}\noutsize {p} 128x128 pad 255,0,0,128 top-left\ndump\nconvertall\nidle\ndump\n"
                      f"paainfo {p}\noutsize {p} off\ndump\n")
    assert r.returncode == 0, r.stderr
    set_, done, info, off = blocks(r.stdout)
    assert row(set_, "door_nohq.png") == (
        "door_nohq.png | max=off ▾ | auto watch=☐ off | needs_converting | 64x64 | output size=128x128 pad top-left #ff000080"
    )
    assert row(done, "door_nohq.png").startswith("door_nohq.png | max=off ▾ | auto watch=☐ off | up_to_date | 64x64 | made_by=loki")
    assert row(done, "door_nohq.png").endswith("| output size=128x128 pad top-left #ff000080")
    assert info[0].endswith("128x128")
    assert "output size" not in row(off, "door_nohq.png")  # Source: nothing shown
    assert "needs_converting: PAA is 128x128" in row(off, "door_nohq.png")  # back at the source size


def test_scenario_exclusive_with_max_size_and_says_so(tmp_path):
    p = png(tmp_path / "tex" / "a.png", (512, 256))
    r = run(tmp_path, f"drop {p}\nmaxsize {p} 256\noutsize {p} 64x64\ndump\nmaxsize {p} 128\n"
                      f"maxsize {p} 512\ndump\n")
    assert r.returncode == 2 and "unsupported" not in r.stdout  # 128 is not a max size choice: a scenario error
    r = run(tmp_path, f"drop {p}\nmaxsize {p} 256\noutsize {p} 64x64\ndump\nmaxsize {p} 512\ndump\n")
    assert r.returncode == 0, r.stderr
    sized, capped = blocks(r.stdout)
    line = row(sized, "a.png")
    assert "max=off" in line and "output size=64x64 contain" in line
    assert f"status: {app_mod.SIZE_REPLACED_HINT}" in sized
    line = row(capped, "a.png")
    assert "max=512" in line and "output size" not in line
    assert f"status: {app_mod.MAX_REPLACED_HINT}" in capped


def test_scenario_non_power_of_two_row_becomes_convertible(tmp_path):
    p = png(tmp_path / "tex" / "odd.png", (50, 30))
    r = run(tmp_path, f"converter loki\ndrop {p}\ndump\noutsize {p} 64x32 stretch\ndump\nconvertall\nidle\ndump\npaainfo {p}\n")
    assert r.returncode == 0, r.stderr
    before, sized, done, info = blocks(r.stdout)
    assert "flagged: 50x30: sides must be powers of two" in row(before, "odd.png")
    assert "or set an output size" in row(before, "odd.png")
    assert "needs_converting" in row(sized, "odd.png") and "flagged" not in row(sized, "odd.png")
    assert "up_to_date" in row(done, "odd.png")
    assert info[0].endswith("64x32")


def test_scenario_restart_keeps_the_setting(tmp_path):
    p = png(tmp_path / "tex" / "a.png")
    r = run(tmp_path, f"drop {p}\noutsize {p} 256x128 contain 0,255,0,255 bottom\nrestart\ndump\n")
    assert r.returncode == 0, r.stderr
    (d,) = blocks(r.stdout)
    assert row(d, "a.png").endswith("output size=256x128 contain bottom #00ff00ff")
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["files"][0]
    assert saved["size"] == {"width": 256, "height": 128, "fit": "contain", "colour": [0, 255, 0, 255], "align": "bottom"}


def test_scenario_bad_outsize_is_a_plain_error(tmp_path):
    p = png(tmp_path / "tex" / "a.png")
    r = run(tmp_path, f"drop {p}\noutsize {p} 300x200\n")
    assert r.returncode == 2 and "powers of two" in r.stderr


# --- the model -------------------------------------------------------------------------------------------------


def test_cell_text_is_compact_and_short():
    label = app_mod.out_size_label
    assert label(None) == "Source"
    assert label(core.output_size("512x256")) == "512x256 contain"
    assert label(core.output_size("512x256", "stretch", "#ff0000", "top")) == "512x256 stretch"  # stretch pads nothing
    assert label(core.output_size("64x64", "pad", None, "top-left")) == "64x64 pad top-left"
    assert label(core.output_size("64x64", "pad", "#10203040")) == "64x64 pad #10203040"
    row = app_mod.Row("k", "x.png", None, False, core.UP_TO_DATE, None, (1, 1))
    assert row.out_cell == "Source ▾"


def test_set_output_size_applies_to_every_selected_row_and_clears_max(tmp_path):
    ps = [png(tmp_path / "t" / f"{n}.png") for n in "abc"]
    app = make_app(tmp_path)
    app.add_files(ps)
    keys = [r.key for r in app.rows]
    app.set_max_size(keys[:1], 256)
    size = core.output_size("128x128", "pad")
    app.set_output_size(keys[:2], size)
    rows = {r.name: r for r in app.rows}
    assert rows["a.png"].out_size == rows["b.png"].out_size == size and rows["c.png"].out_size is None
    assert rows["a.png"].max_size is None  # cleared
    assert app.message == app_mod.SIZE_REPLACED_HINT
    app.set_output_size(keys[1:2], None)
    assert app.message == "" and {r.name: r.out_size for r in app.rows}["b.png"] is None
    assert app.selected_output_size(keys[:1]) == size
    app.close()


def test_changed_setting_needs_converting_like_a_changed_max_size(tmp_path):
    p = png(tmp_path / "t" / "a.png")
    app = make_app(tmp_path)
    app.add_files([p])
    key = app.rows[0].key
    app.set_output_size([key], core.output_size("128x128"))
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    assert app.rows[0].status == core.UP_TO_DATE and core.paa_size(p.with_suffix(".paa")) == (128, 128)
    app.set_output_size([key], core.output_size("128x128", "stretch"))  # same size, other fit
    assert app.rows[0].status == core.NEEDS_CONVERTING
    app.set_output_size([key], core.output_size("128x128"))
    assert app.rows[0].status == core.UP_TO_DATE  # back to what the .paa was made with
    app.close()


def test_auto_watch_converts_at_the_rows_output_size(tmp_path):
    p = png(tmp_path / "t" / "a.png")
    app = make_app(tmp_path)
    app.add_files([p])
    wait_for(app, app.watching)
    app.set_output_size([app.rows[0].key], core.output_size("32x16"))
    app.toggle([app.rows[0].key])
    time.sleep(0.2)
    p.write_bytes(p.read_bytes())
    os.utime(p)  # a save
    wait_for(app, lambda: core.paa_size(p.with_suffix(".paa")) == (32, 16) and app.quiet())
    assert app.rows[0].status == core.UP_TO_DATE
    app.close()


def test_sort_by_output_size_uses_pixel_count_with_source_largest(tmp_path):
    ps = [png(tmp_path / "t" / f"{n}.png") for n in "abc"]
    app = make_app(tmp_path)
    app.add_files(ps)
    keys = {r.name: r.key for r in app.rows}
    app.set_output_size([keys["a.png"]], core.output_size("256x4"))  # 1024 px
    app.set_output_size([keys["b.png"]], core.output_size("64x64"))  # 4096 px
    app.sort_by("Output size", False)
    assert [r.name for r in app.rows] == ["a.png", "b.png", "c.png"]
    app.sort_by("outsize", True)
    assert [r.name for r in app.rows] == ["c.png", "b.png", "a.png"]
    assert app.header_text("outsize") == "Output size ▼"
    app.close()


# --- settings ------------------------------------------------------------------------------------------------------


def test_a_0_3_0_settings_file_loads_unchanged(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({
        "version": 2, "converter": "loki", "imagetopaa_path": None,
        "files": [{"path": str(tmp_path / "a.png"), "max_size": 512, "bake": True, "made_by": "loki"}],
    }), encoding="utf-8")
    (item,) = Settings.load(f).files()
    assert item.size is None and item.max_size == 512 and item.bake and item.made_by == "loki"


def test_settings_round_trip_and_bad_entries_are_ignored(tmp_path):
    f = tmp_path / "s.json"
    s = Settings.load(f)
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    s.add_files([a, b])
    keys = [x.key for x in s.files()]
    s.set_size(keys[:1], core.output_size("512x256", "pad", "10,20,30,40", "right"))
    assert Settings.load(f).files()[0].size == core.output_size("512x256", "pad", "10,20,30,40", "right")
    assert Settings.load(f).files()[1].size is None
    data = json.loads(f.read_text(encoding="utf-8"))
    data["files"][1]["size"] = {"width": 300, "height": 4, "fit": "contain"}  # not a power of two
    data["files"][0]["max_size"] = 256  # both set: the size wins
    f.write_text(json.dumps(data), encoding="utf-8")
    loaded = Settings.load(f).files()
    assert loaded[1].size is None and loaded[0].max_size is None and loaded[0].size is not None


# --- the dialog -------------------------------------------------------------------------------------------------------


def test_dialog_builds_the_size_with_the_core_validation(tk_root):
    dlg = gui.OutputSizeDialog(tk_root, None, "wall_nohq.png")
    assert dlg.result() is None  # Source
    assert dlg.swatch.cget("background") != "" and dlg.colour_note.cget("text") == "type default"
    dlg.width_var.set("512")
    dlg._sync(dlg.width_var, dlg.height_var)
    assert dlg.height_var.get() == "512"  # a size next to Source gives the other side the same size
    dlg.height_var.set("256")
    dlg.fit_var.set("pad")
    dlg.align_var.set("bottom-right")
    assert dlg.result() == core.output_size("512x256", "pad", None, "bottom-right")  # colour untouched: the type default
    dlg.set_colour((1, 2, 3, 4))
    assert dlg.result().colour == (1, 2, 3, 4) and dlg.colour_note.cget("text") == ""
    dlg.width_var.set("Source")
    dlg._sync(dlg.width_var, dlg.height_var)
    assert dlg.height_var.get() == "Source" and dlg.result() is None
    assert set(dlg.align_buttons) == set(core.ALIGNS)  # the 3x3 grid has every alignment
    dlg.top.destroy()


def test_dialog_alpha_change_makes_the_default_colour_explicit(tk_root):
    dlg = gui.OutputSizeDialog(tk_root, None, "x_co.png")
    default = core.OutputSize(1, 1).resolved_colour("x_co.png")
    dlg.alpha_var.set(7)
    dlg._alpha_changed()
    assert dlg._colour == (*default[:3], 7)
    dlg.top.destroy()


def test_window_cell_click_applies_the_dialog_to_the_selection(tk_root, tmp_path, monkeypatch):
    ps = [png(tmp_path / "t" / f"{n}.png") for n in "abc"]
    app = make_app(tmp_path)
    win = gui.MainWindow(app, tk_root)
    app.add_files(ps)
    win._tick()
    seen = {}

    def fake_show(self):  # the modal loop is not run: pick a size as OK would
        seen["count"] = self.top.title()
        self.size = core.output_size("128x64", "stretch")
        self.accepted = True
        self.top.destroy()
        return True

    monkeypatch.setattr(gui.OutputSizeDialog, "show", fake_show)
    ids = list(win.tree.get_children())
    win.tree.selection_set(ids[:2])
    win.edit_output_size(win._key_of[ids[0]])
    assert "2 files" in seen["count"]
    cells = [win.tree.item(i, "values")[5] for i in win.tree.get_children()]
    assert cells == ["128x64 stretch ▾", "128x64 stretch ▾", "Source ▾"]
    win.edit_output_size(win._key_of[ids[2]])  # not in the selection: only that row
    assert [win.tree.item(i, "values")[5] for i in win.tree.get_children()] == ["128x64 stretch ▾"] * 3
    app.close()
