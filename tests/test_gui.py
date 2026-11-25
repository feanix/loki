"""Tk smoke tests for gui.MainWindow. The window stays withdrawn (never shown, never focused).
A Tk that cannot start fails (see conftest.py; LOKI_ALLOW_TK_SKIP=1 skips)."""

import itertools
import time
import tkinter as tk
from pathlib import Path

import pytest
from PIL import Image

from conftest import tk_unavailable, track_app
from loki import app as app_mod
from loki import core, gui
from paa_helpers import paa_for_png


def pngs(folder: Path, n: int) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        out.append(folder / f"t{i:02}.png")
        Image.new("RGBA", (32, 32), (1, 2, 3, 255)).save(out[-1])
    return out


def pump_tk(root, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        root.update()
        time.sleep(0.02)
    assert cond(), "timed out"


def make_win(tk_root, tmp_path):
    app = app_mod.App(tmp_path / "s.json", find_tool=lambda _o: Path("t.exe"), interval=0.02, settle=0.1)
    track_app(app)
    return app, gui.MainWindow(app, tk_root)


def slow_convert(png, tool, max_size=None, converter=None):
    time.sleep(0.02)
    paa = Path(png).with_suffix(".paa")
    paa.write_bytes(paa_for_png(png, max_size))
    return core.ConvertResult(Path(png), paa, True, None)


def test_window_lists_rows_and_toggles(tk_root, tmp_path):
    files = pngs(tmp_path / "tex", 3)
    app, win = make_win(tk_root, tmp_path)
    assert win.empty_label.winfo_manager() == "place"  # empty list shows the drag hint
    assert win.empty_label.cget("text") == "Drag PNG files here"
    app.add_files(files)
    win._tick()
    assert win.empty_label.winfo_manager() == ""
    ids = list(win.tree.get_children())
    assert [win.tree.item(i, "values")[1] for i in ids] == ["t00.png", "t01.png", "t02.png"]
    assert win.tree.item(ids[0], "values")[0] == "☐ Off"
    heads = [win.tree.heading(c, "text") for c in win.tree["columns"]]
    assert heads == [n + (" ▲" if n == "File" else "") for n in app_mod.COLUMNS]  # default sort: File ascending
    values = win.tree.item(ids[0], "values")
    assert values[2] == app_mod.folder_label(str(tmp_path / "tex")) and values[3] == "32x32" and values[4] == "Off ▾"
    win.tree.selection_set(ids[:2])
    win._on_space(None)
    assert [win.tree.item(i, "values")[0] for i in win.tree.get_children()] == ["☑ On", "☑ On", "☐ Off"]
    assert str(win.convert_btn.cget("text")) == "Convert all"
    assert not win.tool_row.winfo_manager()  # tool found: warning row hidden
    assert not hasattr(win, "choose_folder") and not hasattr(win, "sub_var")
    app.close()


def test_buttons_match_the_digest_controls(tk_root, tmp_path):
    from tkinter import ttk

    app, _win = make_win(tk_root, tmp_path)
    texts = []

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, ttk.Button) and str(c.cget("text")) != "Browse...":
                texts.append(str(c.cget("text")))
            walk(c)

    walk(tk_root)
    assert sorted(texts) == sorted(c for c in app_mod.CONTROLS if c != "Converter")  # the dropdown is no button
    app.close()


def test_delete_key_and_remove_button_remove_selected_rows_only(tk_root, tmp_path):
    files = pngs(tmp_path / "tex", 4)
    app, win = make_win(tk_root, tmp_path)
    app.add_files(files)
    win._tick()
    ids = list(win.tree.get_children())
    win.tree.selection_set([ids[0], ids[2]])
    win._on_delete()
    assert [win.tree.item(i, "values")[1] for i in win.tree.get_children()] == ["t01.png", "t03.png"]
    win.tree.selection_set(win.tree.get_children()[0])
    win.remove_btn.invoke()
    assert [win.tree.item(i, "values")[1] for i in win.tree.get_children()] == ["t03.png"]
    assert all(f.exists() for f in files)  # the pngs are never touched
    assert [r.name for r in app.rows] == ["t03.png"]
    win._on_delete()  # nothing selected: a no-op, not an error
    app.close()


def test_max_size_menu_changes_only_that_row(tk_root, tmp_path):
    files = pngs(tmp_path / "tex", 2)
    app, win = make_win(tk_root, tmp_path)
    app.add_files(files)
    win._tick()
    first, second = list(win.tree.get_children())
    win.set_row_max(win._key_of[second], 256)
    assert win.tree.item(first, "values")[4] == "Off ▾" and win.tree.item(second, "values")[4] == "256 ▾"
    assert [r.max_size for r in app.rows] == [None, 256]
    app.close()


def test_drop_message_reaches_a_window_through_the_real_window_procedure(tk_root, tmp_path):
    import sys

    if sys.platform != "win32":
        pytest.skip("WM_DROPFILES is Windows only")
    from loki import dnd

    got = []
    hwnd = int(tk_root.frame(), 16)  # the top-level window, as Explorer would target it
    target = dnd.install(hwnd, got.append)
    odd = tmp_path / "t\u00e9 x"  # a non-ASCII path with a space must survive the round trip
    dnd.post_drop(hwnd, [str(odd / "a.png"), str(tmp_path / "b.png")])
    pump_tk(tk_root, lambda: got)
    assert got == [[str(odd / "a.png"), str(tmp_path / "b.png")]]
    target.uninstall()
    dnd.post_drop(hwnd, [str(tmp_path / "c.png")])
    for _ in range(10):
        tk_root.update()
        time.sleep(0.01)
    assert len(got) == 1  # uninstalled: the drop no longer reaches the handler


def test_dropping_files_on_the_window_adds_rows(tk_root, tmp_path):
    import sys

    if sys.platform != "win32":
        pytest.skip("WM_DROPFILES is Windows only")
    from loki import dnd

    files = pngs(tmp_path / "tex", 2)
    (tmp_path / "tex" / "note.txt").write_text("x")
    app, win = make_win(tk_root, tmp_path)
    dnd.post_drop(win.top_hwnd(), [str(files[0]), str(tmp_path / "tex" / "note.txt"), str(files[1])])
    pump_tk(tk_root, lambda: len(win.tree.get_children()) == 2)
    assert [win.tree.item(i, "values")[1] for i in win.tree.get_children()] == ["t00.png", "t01.png"]
    assert win.status_var.get() == "Added 2 files; skipped 1 not PNG"
    # a drop on a widget inside the window lands too (each widget is hooked)
    dnd.post_drop(int(win.tree.winfo_id()), [str(files[0])])
    pump_tk(tk_root, lambda: "1 already listed" in win.status_var.get())
    assert len(win.tree.get_children()) == 2
    app.close()


def test_missing_tool_shows_warning_row(tk_root, tmp_path):
    def nope(_o):
        raise core.ToolNotFound("x")

    app = app_mod.App(tmp_path / "s.json", find_tool=nope)
    app.set_converter(core.BOHEMIA)  # with the Loki converter the banner waits for a row that needs the tool
    win = gui.MainWindow(app, tk_root)
    assert win.tool_row.winfo_manager() == "pack"
    assert "ImageToPAA.exe not found" in win.tool_label.cget("text")
    app.close()


def test_ui_stays_responsive_during_convert_all_of_50(tk_root, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "convert", slow_convert)
    files = pngs(tmp_path / "tex", 50)
    app = app_mod.App(tmp_path / "s.json", find_tool=lambda _o: Path("t.exe"), interval=0.05, settle=0.2, workers=1)
    win = gui.MainWindow(app, tk_root)
    app.add_files(files)
    win._tick()
    ticks = []

    def ticker():
        ticks.append(time.monotonic())
        tk_root.after(50, ticker)

    tk_root.after(50, ticker)
    t0 = time.monotonic()
    win.convert_btn.invoke()
    assert time.monotonic() - t0 < 0.2  # the click returns at once
    assert str(win.convert_btn.cget("text")) == "Convert all"
    end = time.monotonic() + 30
    status_seen = set()
    while time.monotonic() < end:
        tk_root.update()
        status_seen.add(win.status_var.get())
        if not app.converting and app.pump() is False and app.summary().startswith("Converted"):
            break
        time.sleep(0.005)
    assert len(ticks) >= 8, f"Tk loop starved: only {len(ticks)} ticks during ~1 s of conversion"
    gaps = [b - a for a, b in itertools.pairwise(ticks)]
    assert max(gaps) < 0.5
    assert any(s.startswith("Converting ") for s in status_seen)
    assert win.status_var.get() == "Converted 50 file(s)"
    assert all(win.tree.item(i, "values")[6] == "Up to date" for i in win.tree.get_children())
    app.close()


def test_screenshot_renders_the_window_and_posts_a_real_drop(tmp_path):
    import sys

    if sys.platform != "win32":
        pytest.skip("PrintWindow is Windows only")
    files = pngs(tmp_path / "tex", 4)
    out = tmp_path / "shot.png"
    printed = []
    try:
        gui.screenshot(str(tmp_path / "s.json"), out, drop=[str(f) for f in files], echo=printed.append)
    except tk.TclError as exc:
        tk_unavailable(exc)
    digest = "\n".join(printed)
    assert all(str(f) in digest for f in files) and "files: 4" in digest
    with Image.open(out) as im:
        assert im.size[0] >= 600 and im.size[1] >= 300
        assert (
            len(im.convert("RGB").getcolors(maxcolors=1 << 24)) > 200
        )  # client area painted, not just the title bar


def test_header_click_sorts_shows_arrow_and_keeps_selection(tk_root, tmp_path):
    files = pngs(tmp_path / "tex", 3)
    app, win = make_win(tk_root, tmp_path)
    app.add_files(files)
    win._tick()
    first_iid = win.tree.get_children()[0]
    win.tree.selection_set(first_iid)
    win._on_select()
    win._on_header("file")  # a click on the sorted column reverses it
    ids = list(win.tree.get_children())
    assert [win.tree.item(i, "values")[1] for i in ids] == ["t02.png", "t01.png", "t00.png"]
    assert win.tree.heading("file", "text") == "File ▼"
    assert win.tree.heading("size", "text") == "Size"  # others show no arrow
    assert win.tree.selection() == (first_iid,) and ids[-1] == first_iid  # selection follows its row
    win._on_header("size")
    assert win.tree.heading("size", "text") == "Size ▲" and win.tree.heading("file", "text") == "File"


def test_tk_init_failure_fails_unless_skip_allowed(monkeypatch):
    import conftest

    def boom():
        raise tk.TclError("Can't find a usable init.tcl")

    monkeypatch.setattr(conftest.tk, "Tk", boom)
    monkeypatch.delenv("LOKI_ALLOW_TK_SKIP", raising=False)
    with pytest.raises(pytest.fail.Exception, match="init.tcl"):
        conftest.make_tk()
    monkeypatch.setenv("LOKI_ALLOW_TK_SKIP", "1")
    with pytest.raises(pytest.skip.Exception, match="init.tcl"):
        conftest.make_tk()
