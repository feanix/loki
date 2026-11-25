"""The built-in Loki converter: routing, in-process conversion through core.convert and
the app's pool, the converter choice and its persistence, and the no-ImageToPAA behaviour."""

import json
import time
from pathlib import Path

import pytest
from PIL import Image

from loki import app as app_mod
from loki import core, gui
from loki.settings import Settings
from paa_helpers import paa_bytes

FAKE_TOOL = Path("fake-ImageToPAA.exe")


def make_png(path: Path, size=(64, 64), mode="RGB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.effect_noise(size, 50).convert(mode).save(path)
    return path


def tool_gone(_override):
    raise core.ToolNotFound("ImageToPAA not found: forced missing")


def make_app(tmp_path, find_tool=lambda _o: FAKE_TOOL, workers=2):
    return app_mod.App(tmp_path / "settings.json", find_tool=find_tool, interval=0.02, settle=0.1, workers=workers)


def run_all(app, timeout=60.0):
    assert app.convert_all()
    end = time.monotonic() + timeout
    while app.converting:
        app.pump()
        assert time.monotonic() < end, "convert all timed out"
        time.sleep(0.01)
    app.pump()


def fake_tool_run(tool, src, dst):
    dst.write_bytes(paa_bytes(*Image.open(src).size))


def by_name(app):
    return {r.name: r for r in app.rows}


# --- routing -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "converter", "tool", "want"),
    [
        ("a_co.png", core.LOKI, False, core.LOKI),
        ("a_ca.png", core.LOKI, False, core.LOKI),
        ("a.png", core.LOKI, False, core.LOKI),
        ("a_cat.png", core.LOKI, True, core.BOHEMIA),
        ("a_cat.png", core.LOKI, False, core.NEEDS_TOOL),
        ("a_sky.png", core.LOKI, False, core.LOKI),  # `_sky` passes the relative checks, Loki encodes it
        ("a_co.png", core.BOHEMIA, True, core.BOHEMIA),
        ("a_co.png", core.BOHEMIA, False, core.NEEDS_TOOL),
    ],
)
def test_route(name, converter, tool, want):
    """Size plays no part: a PNG over 4096 is Loki's too."""
    assert core.route(name, converter, tool) == want


def test_needs_tool_texts():
    assert core.needs_tool_reason("x_cat.png") == "needs Arma 3 Tools for _cat"
    assert core.needs_tool_reason("x_co.png", core.BOHEMIA) == "ImageToPAA.exe not found"


# --- core.convert ------------------------------------------------------------------------------------


def test_loki_converts_without_any_tool(tmp_path):
    png = make_png(tmp_path / "a_co.png", (128, 64))
    res = core.convert(png, None, None, core.LOKI)
    assert res.ok and res.converter == core.LOKI
    assert core.paa_size(png.with_suffix(".paa")) == (128, 64)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".loki-tmp")] == []


def test_loki_max_size_halves_like_target_size(tmp_path):
    png = make_png(tmp_path / "a.png", (256, 128))
    assert core.convert(png, None, 64, core.LOKI).ok
    assert core.paa_size(png.with_suffix(".paa")) == core.target_size(256, 128, 64) == (64, 32)


def test_loki_route_refuses_unsupported_suffix_without_tool_and_writes_nothing(tmp_path):
    png = make_png(tmp_path / "a_cat.png")
    res = core.convert(png, None, None, core.LOKI)
    assert not res.ok and res.error == "needs Arma 3 Tools for _cat" and res.converter is None
    assert not png.with_suffix(".paa").exists()


def test_loki_sends_other_suffixes_to_the_tool_but_encodes_a_huge_png_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_tool", fake_tool_run)
    cat = make_png(tmp_path / "a_cat.png")
    res = core.convert(cat, FAKE_TOOL, None, core.LOKI)
    assert res.ok and res.converter == core.BOHEMIA
    huge = tmp_path / "big_co.png"
    Image.new("RGB", (8192, 64), (90, 120, 150)).save(huge)
    res = core.convert(huge, None, None, core.LOKI)  # no tool: Loki halves it to 4096
    assert res.ok and res.converter == core.LOKI and core.paa_size(huge.with_suffix(".paa")) == (4096, 32)
    res = core.convert(huge, None, 1024, core.LOKI)  # the user's max size applies on top
    assert res.ok and core.paa_size(huge.with_suffix(".paa")) == (1024, 8) == core.target_size(8192, 64, 1024)



def test_bohemia_selected_sends_everything_to_the_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_tool", fake_tool_run)
    png = make_png(tmp_path / "a_co.png")
    res = core.convert(png, FAKE_TOOL, None, core.BOHEMIA)
    assert res.ok and res.converter == core.BOHEMIA


def test_a_corrupt_png_is_an_error_result_and_loki_keeps_working(tmp_path):
    bad = tmp_path / "bad_co.png"
    bad.write_bytes(b"not a png at all")
    assert not core.convert(bad, None, None, core.LOKI).ok
    good = make_png(tmp_path / "ok_co.png")
    assert core.convert(good, None, None, core.LOKI).ok


# --- settings ----------------------------------------------------------------------------------------


def test_settings_default_save_load_and_bad_values(tmp_path):
    p = tmp_path / "s.json"
    s = Settings.load(p)
    assert s.converter == core.LOKI
    s.add_files([tmp_path / "x.png"])
    key = s.files()[0].key
    s.set_converter(core.BOHEMIA)
    s.set_made_by(key, core.LOKI)
    again = Settings.load(p)
    assert again.converter == core.BOHEMIA and again.files()[0].made_by == core.LOKI
    with pytest.raises(ValueError):
        s.set_converter("nope")
    data = json.loads(p.read_text(encoding="utf-8"))
    data["converter"] = "nope"
    data["files"][0]["made_by"] = 7
    p.write_text(json.dumps(data), encoding="utf-8")
    bad = Settings.load(p)
    assert bad.converter == core.LOKI and bad.files()[0].made_by is None


# --- the app -----------------------------------------------------------------------------------------


def test_convert_all_runs_the_encoder_in_the_pool_without_the_tool(tmp_path):
    names = [f"t{i}_co.png" for i in range(6)]
    for n in names:
        make_png(tmp_path / n, (256, 256))
    app = make_app(tmp_path, tool_gone, workers=3)
    try:
        assert app.converter == core.LOKI and not app.tool_found
        app.add_files([tmp_path / n for n in names])
        assert not app.tool_warning  # nothing needs the tool, so no warning
        run_all(app)
        assert app.summary() == "Converted 6 file(s)"
        rows = by_name(app)
        got = [(n, rows[n].status, rows[n].reason, rows[n].made_by) for n in names]
        assert all(g[1] == core.UP_TO_DATE and g[3] == core.LOKI for g in got), got
        assert rows[names[0]].status_text == "Up to date (Loki)"
        assert not app.tool_warning
    finally:
        app.close()


def test_mixed_suffixes_tool_absent_and_present(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_tool", fake_tool_run)
    files = {n: make_png(tmp_path / n) for n in ("a_co.png", "b_ca.png", "c.png", "d_cat.png", "e_can.png")}
    app = make_app(tmp_path, tool_gone)
    try:
        app.add_files(list(files.values()))
        rows = by_name(app)
        assert rows["d_cat.png"].status == core.NEEDS_TOOL
        assert rows["d_cat.png"].status_text == "needs Arma 3 Tools for _cat"
        assert rows["e_can.png"].status_text == "needs Arma 3 Tools for _can"
        assert rows["a_co.png"].status == core.NEEDS_CONVERTING
        assert app.tool_warning  # a row needs it now
        run_all(app)
        assert app.summary() == "Converted 3 file(s), 2 need Arma 3 Tools"
        rows = by_name(app)
        assert [rows[n].status for n in ("a_co.png", "b_ca.png", "c.png")] == [core.UP_TO_DATE] * 3
        assert rows["d_cat.png"].status == core.NEEDS_TOOL
        assert not (tmp_path / "d_cat.paa").exists()
        assert "needs_tool: needs Arma 3 Tools for _cat" in app.digest()
    finally:
        app.close()
    app = make_app(tmp_path)  # ImageToPAA present (fake): the same rows now convert
    try:
        app.add_files(list(files.values()))
        assert by_name(app)["d_cat.png"].status == core.NEEDS_CONVERTING
        run_all(app)
        rows = by_name(app)
        assert rows["d_cat.png"].made_by == core.BOHEMIA and rows["d_cat.png"].status_text == "Up to date (Bohemia)"
        assert rows["a_co.png"].made_by == core.LOKI
        lines = app.digest().splitlines()
        assert "  converter: loki" in lines
        assert any(ln.endswith("| made_by=bohemia") and "d_cat" in ln for ln in lines)
    finally:
        app.close()


def test_png_over_4096_is_encoded_by_loki_with_no_tool(tmp_path):
    huge = tmp_path / "huge_co.png"
    Image.new("RGB", (8192, 64), (90, 120, 150)).save(huge)
    app = make_app(tmp_path, tool_gone)
    try:
        app.add_files([huge])
        assert app.rows[0].status != core.NEEDS_TOOL
        run_all(app)
        assert app.rows[0].made_by == core.LOKI and app.rows[0].status == core.UP_TO_DATE
        assert core.paa_size(huge.with_suffix(".paa")) == (4096, 32)
    finally:
        app.close()


def test_bohemia_selected_without_the_tool_warns_and_does_not_convert(tmp_path):
    png = make_png(tmp_path / "a_co.png")
    app = make_app(tmp_path, tool_gone)
    try:
        app.add_files([png])
        app.set_converter(core.BOHEMIA)
        assert app.tool_warning
        assert app.rows[0].status == core.NEEDS_CONVERTING  # Bohemia + no tool: the banner speaks, not the row
        assert app.convert_all() is False and "ImageToPAA not found" in app.message
    finally:
        app.close()


def test_switching_converter_persists_and_never_changes_status(tmp_path):
    png = make_png(tmp_path / "a_co.png")
    app = make_app(tmp_path, tool_gone)
    app.add_files([png])
    run_all(app)
    before = [(r.status, r.reason) for r in app.rows]
    app.set_converter(core.BOHEMIA)
    assert [(r.status, r.reason) for r in app.rows] == before == [(core.UP_TO_DATE, None)]
    app.set_converter(core.LOKI)
    app.set_converter(core.BOHEMIA)
    app.close()
    again = make_app(tmp_path, tool_gone)
    try:
        assert again.converter == core.BOHEMIA
        assert again.rows[0].status == core.UP_TO_DATE and again.rows[0].made_by == core.LOKI
        again.set_converter("bogus")
        assert again.converter == core.BOHEMIA and "unknown converter" in again.message
    finally:
        again.close()


# --- the window --------------------------------------------------------------------------------------


def test_dropdown_selects_the_converter_and_banner_follows_need(tk_root, tmp_path):
    png = make_png(tmp_path / "x_cat.png")
    app = make_app(tmp_path, tool_gone)
    win = gui.MainWindow(app, tk_root)
    try:
        assert list(win.converter_box.cget("values")) == ["Loki converter", "Bohemia ImageToPAA"]
        assert win.converter_var.get() == "Loki converter"
        assert win.tool_row.winfo_manager() == ""  # no row needs the tool
        app.add_files([png])
        win._refresh()
        assert win.tool_row.winfo_manager() == "pack"
        win.converter_var.set("Bohemia ImageToPAA")
        win._on_converter()
        assert app.converter == core.BOHEMIA and app.settings.converter == core.BOHEMIA
        values = win.tree.item(win.tree.get_children()[0], "values")
        assert values[-1] == "Needs converting"
    finally:
        app.close()
