"""Model tests for loki.app.App: headless, fake converter (core.convert is monkeypatched), tmp settings.

Real threads are used (watcher, convert-all worker) but with tiny intervals and bounded waits.
The list is files dropped in (decision 11): every test adds files through App.add_files, the one drop handler.
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from loki import app as app_mod
from loki import core
from paa_helpers import paa_bytes, paa_for_png


def make_png(path: Path, size=(64, 64)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, (1, 2, 3, 255)).save(path)
    return path


@pytest.fixture
def tree(tmp_path):
    """Pngs in two folders, plus a non power of two one and noise that a drop must ignore."""
    root = tmp_path / "tex"
    for rel in ("a.png", "b.png", "other/c.png"):
        make_png(root / rel)
    make_png(root / "odd.png", (60, 40))
    return root


def fake_tool(_override):
    return Path("fake-ImageToPAA.exe")


def no_tool(_override):
    raise core.ToolNotFound("ImageToPAA not found: test")


def fake_convert(delay=0.0, fail=(), caps=None):
    def convert(png, tool, max_size=None, converter=None):
        time.sleep(delay)
        png = Path(png)
        if caps is not None:
            caps.append((png.name, max_size))
        if png.name in fail:
            return core.ConvertResult(png, png.with_suffix(".paa"), False, "boom")
        png.with_suffix(".paa").write_bytes(paa_for_png(png, max_size))
        return core.ConvertResult(png, png.with_suffix(".paa"), True, None)

    return convert


def wait_for(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        app.pump()
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)
    app.pump()


def make_app(tmp_path, find_tool=fake_tool, workers=None):
    app = app_mod.App(tmp_path / "settings.json", find_tool=find_tool, interval=0.02, settle=0.1, workers=workers)
    app.set_converter(core.BOHEMIA)  # these tests drive fake tools; the Loki converter has its own tests
    return app


def drop(app, *paths):
    """A drop, then wait until the watcher has baselined the new rows (a later save is a real save)."""
    added = app.add_files(paths)
    wait_for(app, app.watching)
    return added


def by_name(app):
    return {r.name: r for r in app.rows}


def listing(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_defaults_have_no_folder_anywhere(tmp_path):
    app = make_app(tmp_path)
    assert app.rows == [] and app.summary() == "Drag PNG files onto the window to begin."
    assert app_mod.MAX_CHOICES == (None, 2048, 1024, 512, 256)
    assert app.settings.files() == []
    assert not hasattr(app, "folder") and not hasattr(app.settings, "folder")
    assert "Choose folder" not in app.digest() and "subfolders" not in app.digest()
    app.close()


def test_drop_adds_a_row_per_file_from_any_folder_and_writes_nothing(tmp_path, tree, monkeypatch):
    monkeypatch.setattr(core, "convert", fake_convert())
    before = listing(tree)
    app = make_app(tmp_path)
    added = drop(app, tree / "a.png", tree / "other" / "c.png", tree / "odd.png")
    assert len(added) == 3
    time.sleep(0.4)  # let the watcher run a few passes: still nothing may be written
    app.pump()
    assert listing(tree) == before
    rows = by_name(app)
    assert [r.name for r in app.rows] == ["a.png", "c.png", "odd.png"]  # list order = drop order
    assert rows["a.png"].status == core.NEEDS_CONVERTING and rows["a.png"].bake is False
    assert rows["a.png"].max_size is None and rows["a.png"].size_text == "64x64"
    assert rows["c.png"].folder == str(tree / "other")
    assert rows["odd.png"].status == core.FLAGGED and "powers of two" in rows["odd.png"].status_text
    assert app.message == "Added 3 files"
    app.close()


def test_drop_ignores_non_png_folders_missing_and_duplicates_and_counts_them(tmp_path, tree):
    (tree / "notes.txt").write_text("x")
    (tree / "dir.png").mkdir()  # a folder, even one named like a png
    app = make_app(tmp_path)
    app.add_files([tree / "a.png"])
    app.add_files(
        [tree / "b.png", tree / "notes.txt", tree / "other", tree / "dir.png", tree / "ghost.png", tree / "a.png", tree / "B.PNG"]
    )
    assert [r.name for r in app.rows] == ["a.png", "b.png"] + ([] if os.name == "nt" else ["B.PNG"])
    dupes = 2 if os.name == "nt" else 1
    assert app.message == f"Added 1 file; skipped 3 not PNG, 1 not found, {dupes} already listed"
    app.add_files([tree / "a.png"])
    assert app.message == "Added 0 files; skipped 1 already listed"
    assert len(app.settings.files()) == len(app.rows)
    app.close()


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive paths are a Windows rule")
def test_redrop_with_different_case_or_slashes_adds_no_duplicate(tmp_path, tree):
    app = make_app(tmp_path)
    app.add_files([tree / "a.png"])
    app.add_files([str(tree / "A.PNG"), str(tree / "a.png").replace("\\", "/")])
    assert len(app.rows) == 1 and "2 already listed" in app.message
    app.close()


def test_toggle_all_on_off(tmp_path, tree):
    app = make_app(tmp_path)
    app.add_files([tree / "a.png", tree / "b.png", tree / "odd.png"])
    ka, kb = (r.key for r in app.rows[:2])
    app.toggle([ka])
    assert [r.name for r in app.rows if r.bake] == ["a.png"]
    app.toggle([ka, kb])  # mixed selection: both follow the first (a: on -> off)
    assert not any(r.bake for r in app.rows)
    app.set_all(True)
    assert all(r.bake for r in app.rows)
    app.set_all(False)
    assert not any(r.bake for r in app.rows)
    app.close()


def test_max_size_is_per_row_and_validated(tmp_path):
    root = tmp_path / "big"
    wide, other = make_png(root / "wide.png", (512, 256)), make_png(root / "other.png", (512, 256))
    for p in (wide, other):  # both already converted at full size
        os.utime(p, (1000, 1000))
        p.with_suffix(".paa").write_bytes(paa_bytes(512, 256))
    app = make_app(tmp_path)
    app.add_files([wide, other])
    app.set_max_size([by_name(app)["wide.png"].key], 256)  # rows are sorted by name, not in drop order
    rows = by_name(app)
    assert rows["wide.png"].max_size == 256 and rows["other.png"].max_size is None
    assert rows["wide.png"].status == core.NEEDS_CONVERTING
    assert rows["wide.png"].reason == "PAA is 512x256, max size wants 256x128"
    assert rows["other.png"].status == core.UP_TO_DATE  # untouched row keeps its own cap
    app.set_max_size([app.rows[0].key], 999)
    assert by_name(app)["wide.png"].max_size == 256 and "unsupported" in app.message
    assert app_mod.parse_max_size("Off") is None and app_mod.parse_max_size("2048") == 2048
    with pytest.raises(ValueError):
        app_mod.parse_max_size("4096")
    app.close()


def test_convert_all_uses_each_rows_own_cap(tmp_path, monkeypatch):
    caps = []
    monkeypatch.setattr(core, "convert", fake_convert(caps=caps))
    root = tmp_path / "caps"
    a, b = make_png(root / "a.png", (512, 256)), make_png(root / "b.png", (512, 256))
    app = make_app(tmp_path)
    app.add_files([a, b])
    app.set_max_size([app.rows[1].key], 256)
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    assert sorted(caps) == [("a.png", None), ("b.png", 256)]
    assert core.paa_size(a.with_suffix(".paa")) == (512, 256)
    assert core.paa_size(b.with_suffix(".paa")) == (256, 128)
    assert {r.status for r in app.rows} == {core.UP_TO_DATE}
    caps.clear()
    app.set_max_size([app.rows[0].key], 256)  # only a is stale now
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    assert caps == [("a.png", 256)]
    app.close()


def test_remove_leaves_files_alone_and_stops_watching(tmp_path, tree, monkeypatch):
    monkeypatch.setattr(core, "convert", fake_convert())
    a, b = tree / "a.png", tree / "b.png"
    app = make_app(tmp_path)
    drop(app, a, b)
    app.set_all(True)
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    before = listing(tree)
    app.remove([app.rows[0].key])
    assert [r.name for r in app.rows] == ["b.png"] and listing(tree) == before
    assert [os.path.basename(f.path) for f in app.settings.files()] == ["b.png"]
    a.with_suffix(".paa").unlink()
    os.utime(a, ns=(time.time_ns() + 5_000_000_000,) * 2)  # a is saved while off the list
    time.sleep(0.5)
    app.pump()
    assert not a.with_suffix(".paa").exists() and [r.name for r in app.rows] == ["b.png"]
    app.remove(["not-a-key"])  # unknown keys are ignored
    assert len(app.rows) == 1
    app.close()


def test_list_cap_and_bake_survive_restart_and_a_missing_file_recovers(tmp_path, tree):
    a, b = tree / "a.png", tree / "b.png"
    app = make_app(tmp_path)
    drop(app, a, b, tree / "other" / "c.png")
    app.set_max_size([app.rows[1].key], 256)
    app.toggle([app.rows[0].key])
    app.set_tool(__file__)  # any existing file validates through the fake finder
    app.close()
    gone = tmp_path / "hidden.png"
    b.rename(gone)  # b disappears while Loki is closed
    again = make_app(tmp_path)
    assert [(r.name, r.max_size, r.bake) for r in again.rows] == [
        ("a.png", None, True), ("b.png", 256, False), ("c.png", None, False)
    ]  # fmt: skip
    assert again.settings.imagetopaa_path == __file__
    assert by_name(again)["b.png"].status == core.MISSING
    assert by_name(again)["b.png"].status_text == "Missing"
    assert "1 missing" in again.summary()
    gone.rename(b)  # it returns: same row, real status
    wait_for(again, lambda: by_name(again)["b.png"].status == core.NEEDS_CONVERTING)
    assert by_name(again)["b.png"].max_size == 256
    again.close()


def test_a_0_1_0_settings_file_opens_with_an_empty_list(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"folder": "C:/gone", "include_subfolders": True, "max_size": 512, "imagetopaa_path": None,
                    "bake": {"c:\\gone": {"a.png": True}}}),
        encoding="utf-8",
    )  # fmt: skip
    app = make_app(tmp_path)
    assert app.rows == [] and "C:/gone" not in app.digest() and app.settings.files() == []
    app.close()


def test_convert_all_returns_at_once_and_reports_progress(tmp_path, monkeypatch):
    root = tmp_path / "many"
    for i in range(50):
        make_png(root / f"t{i:02}.png")
    # Event-gated: workers block until released, so "returns at once" and "progress is reported" do not
    # depend on wall-clock timing. `gate` holds the first 2 converts; the rest run once it opens.
    gate = threading.Event()
    started = threading.Semaphore(0)
    real = fake_convert()

    def gated(png, tool, max_size=None, converter=None):
        started.release()
        assert gate.wait(30)
        return real(png, tool, max_size, converter)

    monkeypatch.setattr(core, "convert", gated)
    app = make_app(tmp_path, workers=2)
    app.add_files(sorted(root.glob("*.png")))
    assert app.convert_all() is True  # returned while every worker is still blocked: it did not wait for work
    assert app.converting and app.convert_all() is False  # button is disabled while running
    assert started.acquire(timeout=30)  # a worker is mid-convert, nothing finished yet
    app.pump()
    assert app.converting and not any(r.status == core.UP_TO_DATE for r in app.rows)
    gate.set()
    seen = []
    end = time.monotonic() + 30
    while app.converting:
        app.pump()
        if app.progress:
            seen.append(app.progress)
        assert time.monotonic() < end
        time.sleep(0.005)
    assert seen and seen[-1][1] == 50 and len({d for d, _ in seen}) >= 1
    wait_for(app, lambda: all(r.status == core.UP_TO_DATE for r in app.rows))  # row refresh follows the events
    assert len(app.rows) == 50
    assert app.summary() == "Converted 50 file(s)"
    app.close()


def test_convert_with_an_empty_list_says_so(tmp_path):
    app = make_app(tmp_path)
    assert app.convert_all() is False and "Nothing to convert" in app.message
    app.close()


def test_convert_failure_is_shown_on_the_row_and_cleared_on_save(tmp_path, tree, monkeypatch):
    monkeypatch.setattr(core, "convert", fake_convert(fail={"b.png"}))
    app = make_app(tmp_path)
    app.add_files([tree / "a.png", tree / "b.png"])
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    row = by_name(app)["b.png"]
    assert row.status == "error" and row.status_text == "Error: boom"
    assert "1 failed" in app.summary()
    os.utime(tree / "b.png", ns=(time.time_ns() + 5_000_000_000,) * 2)  # the artist saves again
    wait_for(app, lambda: by_name(app)["b.png"].status == core.NEEDS_CONVERTING)
    app.close()


def test_worker_exception_never_crashes(tmp_path, tree, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("tool exploded")

    monkeypatch.setattr(core, "convert", explode)
    app = make_app(tmp_path)
    app.add_files([tree / "a.png"])
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    assert app.summary() == "Converted 0 file(s), 1 failed"
    assert "tool exploded" in by_name(app)["a.png"].status_text
    app.close()


def test_bad_png_is_a_row_not_a_crash(tmp_path, tree):
    bad = tree / "broken.png"
    bad.write_bytes(b"nope")
    app = make_app(tmp_path)
    app.add_files([bad])
    row = by_name(app)["broken.png"]
    assert row.status == core.FLAGGED and row.status_text.startswith("Flagged: cannot read")
    app.close()


def test_summary_counts_needs_tool_rows(tmp_path):
    from PIL import Image

    big = tmp_path / "noise_cat.png"  # a noise suffix is ImageToPAA's (a PNG over 4096 is Loki's since #55)
    Image.new("RGB", (64, 64), (9, 9, 9)).save(big)
    app = make_app(tmp_path, find_tool=no_tool)
    app.set_converter(core.LOKI)
    app.add_files([big])
    app.message = ""
    assert by_name(app)["noise_cat.png"].status == core.NEEDS_TOOL
    assert "0 need converting" in app.summary() and "1 need Arma 3 Tools" in app.summary()
    app.close()


def test_tool_not_found_then_browse(tmp_path, tree):
    app = make_app(tmp_path, find_tool=no_tool)
    assert app.tool_found is False and app.tool_error.startswith("ImageToPAA not found")
    app.add_files([tree / "a.png"])
    assert app.convert_all() is False and "ImageToPAA not found" in app.message
    app.close()

    def finder(override):
        if override and Path(override).is_file():
            return Path(override)
        raise core.ToolNotFound(f"ImageToPAA not found at the configured path: {override}")

    app = make_app(tmp_path, find_tool=finder)
    app.set_tool(tmp_path / "missing.exe")
    assert not app.tool_found and app.settings.imagetopaa_path is None and "not found" in app.message
    app.set_tool(__file__)
    assert app.tool_found and app.settings.imagetopaa_path == __file__ and app.message == ""
    app.close()


def test_watcher_converts_a_saved_png_only_when_toggled(tmp_path, tree, monkeypatch):
    monkeypatch.setattr(core, "convert", fake_convert())
    app = make_app(tmp_path)
    drop(app, tree / "a.png", tree / "b.png")
    app.toggle([app.rows[0].key])
    for name in ("a.png", "b.png"):  # both saved; only a has bake on
        os.utime(tree / name, ns=(time.time_ns() + 5_000_000_000,) * 2)
    wait_for(app, lambda: (tree / "a.paa").exists())
    time.sleep(0.3)
    assert not (tree / "b.paa").exists()
    app.close()


def test_bake_on_change_converts_at_the_rows_own_cap(tmp_path, monkeypatch):
    caps = []
    monkeypatch.setattr(core, "convert", fake_convert(caps=caps))
    root = tmp_path / "big"
    wide, plain = make_png(root / "wide.png", (512, 256)), make_png(root / "plain.png", (512, 256))
    app = make_app(tmp_path)
    drop(app, wide, plain)
    app.set_max_size([by_name(app)["wide.png"].key], 256)  # the closure must read the live cap, no restart
    app.set_all(True)
    for p in (wide, plain):
        os.utime(p, ns=(time.time_ns() + 5_000_000_000,) * 2)
    wait_for(app, lambda: wide.with_suffix(".paa").exists() and plain.with_suffix(".paa").exists())
    assert sorted(caps) == [("plain.png", None), ("wide.png", 256)]
    assert core.paa_size(wide.with_suffix(".paa")) == (256, 128)
    assert core.paa_size(plain.with_suffix(".paa")) == (512, 256)
    app.close()


def test_digest_shape(tmp_path, tree):
    app = make_app(tmp_path)
    app.add_files([tree / "a.png", tree / "odd.png"])
    app.toggle([app.rows[0].key])
    app.set_max_size([app.rows[1].key], 512)
    lines = app.digest().splitlines()
    assert lines[0] == "controls: Converter, Convert all, All on, All off, Remove, About"
    assert lines[1] == "columns: Auto watch, File, Folder, Size, Max size, Output size, Status"
    assert "  tool_found: yes" in lines and "  files: 2" in lines
    assert f"  {tree / 'a.png'} | max=off ▾ | auto watch=☑ on | needs_converting | 64x64" in lines
    assert any(ln.startswith(f"  {tree / 'odd.png'} | max=512 ▾ | auto watch=☐ off | flagged: 60x40") for ln in lines)
    assert not any("folder:" in ln or "subfolders" in ln for ln in lines)
    app.close()


def test_bake_hint_and_selected_row_detail(tmp_path, tree):
    bad = tree / "broken.png"
    bad.write_bytes(b"nope")
    app = make_app(tmp_path)
    app.add_files([tree / "a.png", bad])
    app.toggle([app.rows[0].key])
    assert app.summary() == app_mod.BAKE_HINT
    app.toggle([app.rows[0].key])  # off again: no hint
    assert app.summary() != app_mod.BAKE_HINT
    app.message = ""
    app.selection = [app.rows[1].key]
    assert app.summary().startswith("broken.png: UnidentifiedImageError")
    row = by_name(app)["broken.png"]
    assert row.reason == "cannot read: not a valid PNG" and str(tree) not in row.status_text
    app.close()


def test_convert_all_skips_a_row_removed_mid_run(tmp_path, monkeypatch):
    root = tmp_path / "rm"
    a, b = make_png(root / "a.png"), make_png(root / "b.png")
    app = make_app(tmp_path, workers=1)  # one worker: b is still queued while a runs
    app.add_files([a, b])
    real = fake_convert()
    converted = []

    def convert(png, tool, max_size=None, converter=None):
        converted.append(Path(png).name)
        app.remove([core.file_key(b)])  # the artist removes the second row during the first conversion
        return real(png, tool, max_size, converter)

    monkeypatch.setattr(core, "convert", convert)
    assert app.convert_all() is True
    wait_for(app, lambda: not app.converting)
    assert converted == ["a.png"]
    assert not b.with_suffix(".paa").exists() and a.with_suffix(".paa").exists()
    assert app.summary() == "Converted 1 file(s)"
    app.close()


def test_folder_label_keeps_the_end_of_a_long_path(tmp_path):
    sep = os.sep
    assert app_mod.folder_label("C:" + sep + "a") == "C:" + sep + "a"  # short: untouched
    deep = sep.join(["C:", *(f"level{i}" for i in range(9)), "textures", "weapons"])
    label = app_mod.folder_label(deep)
    assert len(label) == app_mod.FOLDER_LABEL_CHARS
    assert label.startswith("...") and label.endswith(sep + "textures" + sep + "weapons")
    png = make_png(tmp_path.joinpath(*[f"level{i}" for i in range(9)], "textures", "weapons", "g.png"))
    app = make_app(tmp_path)
    app.add_files([png])
    app.selection = [app.rows[0].key]
    app.message = ""  # a notice outranks the path
    assert app.rows[0].folder_text.endswith(sep + "weapons")
    assert app.summary() == app.rows[0].path  # selecting shows the full path
    app.close()


# --- parallel pool (decision 13, #011) ---
def test_app_pool_uses_the_core_worker_limit(tmp_path):
    app = make_app(tmp_path)
    assert app._pool.workers == core.convert_workers()
    app.close()


def test_convert_all_runs_rows_concurrently_up_to_the_limit(tmp_path, monkeypatch):
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    real = fake_convert(delay=0.05)

    def convert(png, tool, max_size=None, converter=None):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            return real(png, tool, max_size, converter)
        finally:
            with lock:
                live["now"] -= 1

    monkeypatch.setattr(core, "convert", convert)
    root = tmp_path / "par"
    for i in range(9):
        make_png(root / f"t{i}.png")
    app = make_app(tmp_path, workers=3)
    app.add_files(sorted(root.glob("*.png")))
    assert app.convert_all() is True
    wait_for(app, lambda: not app.converting)
    assert live["peak"] == 3 and app.summary() == "Converted 9 file(s)"
    app.close()


def test_rows_show_converting_while_busy_and_each_updates_as_it_finishes(tmp_path, monkeypatch):
    gate = {"a.png": threading.Event(), "b.png": threading.Event()}
    real = fake_convert()

    def convert(png, tool, max_size=None, converter=None):
        gate[Path(png).name].wait(10)
        return real(png, tool, max_size, converter)

    monkeypatch.setattr(core, "convert", convert)
    root = tmp_path / "busy"
    a, b = make_png(root / "a.png"), make_png(root / "b.png")
    app = make_app(tmp_path, workers=2)
    app.add_files([a, b])
    app.convert_all()
    assert {r.status_text for r in app.rows} == {"Converting..."}
    gate["a.png"].set()
    wait_for(app, lambda: by_name(app)["a.png"].status_text == "Up to date")
    assert by_name(app)["b.png"].status_text == "Converting..." and app.converting  # b still running
    gate["b.png"].set()
    wait_for(app, lambda: not app.converting)
    assert {r.status_text for r in app.rows} == {"Up to date"}
    app.close()


def test_one_failing_file_among_several_errors_only_its_row_and_leaves_no_temp_dirs(tmp_path, monkeypatch):
    def run(tool, src, dst):
        if dst.name == "bad.paa":
            return "ImageToPAA failed (exit 1): nope"
        dst.write_bytes(paa_bytes(*Image.open(src).size))
        return None

    monkeypatch.setattr(core, "_run_tool", run)
    root = tmp_path / "mix"
    names = ["a", "b", "bad", "c", "d"]
    for n in names:
        make_png(root / f"{n}.png")
    app = make_app(tmp_path, workers=3)
    app.add_files([root / f"{n}.png" for n in names])
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    rows = by_name(app)
    assert rows["bad.png"].status == "error" and "exit 1" in rows["bad.png"].status_text
    assert {rows[f"{n}.png"].status for n in names if n != "bad"} == {core.UP_TO_DATE}
    assert app.summary() == "Converted 4 file(s), 1 failed"
    assert not (root / "bad.paa").exists()
    assert [p.name for p in root.iterdir() if p.name.startswith(".loki-tmp")] == []
    app.close()


def test_last_row_is_not_busy_after_its_result_is_pumped(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "convert", fake_convert())
    root = tmp_path / "last"
    app = make_app(tmp_path, workers=2)
    app.add_files([make_png(root / "a.png"), make_png(root / "b.png")])
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    assert not any(r.busy for r in app.rows) and {r.status_text for r in app.rows} == {"Up to date"}
    app.close()


def test_a_bake_result_does_not_end_a_convert_all_run_for_that_row(tmp_path, monkeypatch):
    gate = threading.Event()
    real = fake_convert()
    calls = []

    def convert(png, tool, max_size=None, converter=None):
        calls.append(Path(png).name)
        if len(calls) == 1:
            gate.wait(10)  # the bake run for a.png holds until Convert all has queued its follow-up
        return real(png, tool, max_size, converter)

    monkeypatch.setattr(core, "convert", convert)
    root = tmp_path / "mixed"
    a = make_png(root / "a.png")
    app = make_app(tmp_path, workers=2)
    app.add_files([a])
    key = app.rows[0].key
    app._pool.submit(key, a, None, "bake")  # as the watcher would
    wait_for(app, lambda: calls == ["a.png"])
    assert app.convert_all() is True  # "all" becomes the follow-up of the running bake
    assert app.progress == (0, 1)
    gate.set()
    wait_for(app, lambda: len(calls) == 2)
    wait_for(app, lambda: not app.converting)
    assert calls == ["a.png", "a.png"] and app.summary() == "Converted 1 file(s)"
    app.close()


def test_a_resave_during_its_own_bake_conversion_converts_twice_never_overlapping(tmp_path, monkeypatch):
    gate = threading.Event()
    real = fake_convert()
    live = {"now": 0, "overlap": False, "calls": 0}
    lock = threading.Lock()

    def convert(png, tool, max_size=None, converter=None):
        with lock:
            live["calls"] += 1
            first = live["calls"] == 1
            live["overlap"] = live["overlap"] or live["now"] > 0
            live["now"] += 1
        try:
            if first:
                gate.wait(10)
            return real(png, tool, max_size, converter)
        finally:
            with lock:
                live["now"] -= 1

    monkeypatch.setattr(core, "convert", convert)
    root = tmp_path / "resave"
    png = make_png(root / "a.png")
    app = make_app(tmp_path, workers=4)
    app.add_files([png])
    app.set_all(True)
    wait_for(app, app.watching)
    os.utime(png, ns=(time.time_ns() + 5_000_000_000,) * 2)
    wait_for(app, lambda: live["calls"] == 1)
    os.utime(png, ns=(time.time_ns() + 10_000_000_000,) * 2)  # saved again while converting
    time.sleep(0.5)  # several watcher passes: still exactly one follow-up
    gate.set()
    wait_for(app, app.idle)
    time.sleep(0.3)
    app.pump()
    assert live["calls"] == 2 and not live["overlap"]
    app.close()


def test_stale_watcher_snapshot_does_not_undo_a_finished_convert(tmp_path, tree, monkeypatch):
    """The watcher reads a row before a convert replaces its .paa; its late event must not win."""
    monkeypatch.setattr(core, "convert", fake_convert())
    a = tree / "a.png"
    app = make_app(tmp_path)
    drop(app, a)
    app.set_max_size([app.rows[0].key], 32)
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    assert app.rows[0].status == core.UP_TO_DATE
    for reason in ("PAA is 64x64, max size wants 32x32", "PAA unreadable"):
        stale = core.PngEntry(a.absolute(), core.NEEDS_CONVERTING, reason, (64, 64))
        app.events.put(("entries", app._gen, [stale], []))
        app.pump()
        assert app.rows[0].status == core.UP_TO_DATE, reason
    a.with_suffix(".paa").unlink()  # a genuinely missing .paa still shows
    app.events.put(("entries", app._gen, [core.PngEntry(a.absolute(), core.NEEDS_CONVERTING, None, (64, 64))], []))
    app.pump()
    assert app.rows[0].status == core.NEEDS_CONVERTING
    app.close()


# --- sorting (#57, decision 49) -----------------------------------------------------------------------------


def srow(path, size=(64, 64), cap=None, bake=False, status=core.UP_TO_DATE, busy=False):
    return app_mod.Row(path.lower(), path, cap, bake, status, None, size, busy=busy)


def names(rows):
    return [r.name for r in rows]


def test_sort_file_and_folder_are_natural_and_case_insensitive():
    rows = [srow("/z/Tex10.png"), srow("/a/tex2.png"), srow("/b/TEX1.png")]
    assert names(app_mod.sort_rows(rows, "file", False)) == ["TEX1.png", "tex2.png", "Tex10.png"]
    assert names(app_mod.sort_rows(rows, "file", True)) == ["Tex10.png", "tex2.png", "TEX1.png"]
    rows = [srow("/d10/a.png"), srow("/D2/a.png")]
    folders = [r.folder for r in app_mod.sort_rows(rows, "folder", False)]
    assert [os.path.basename(f) for f in folders] == ["D2", "d10"]


def test_sort_size_max_bake_status_and_tie_breaks():
    # Size: pixel count (three tie at 4096), then width
    rows = [srow("/x/c.png", (128, 32)), srow("/x/a.png", (64, 64)), srow("/x/b.png", (32, 128)), srow("/x/d.png", (8, 8))]
    assert names(app_mod.sort_rows(rows, "size", False)) == ["d.png", "b.png", "a.png", "c.png"]
    assert names(app_mod.sort_rows(rows, "size", True)) == ["c.png", "a.png", "b.png", "d.png"]
    # Max size: Off counts as largest
    caps = [srow("/x/a.png", cap=None), srow("/x/b.png", cap=256), srow("/x/c.png", cap=2048)]
    assert names(app_mod.sort_rows(caps, "max", False)) == ["b.png", "c.png", "a.png"]
    assert names(app_mod.sort_rows(caps, "max", True)) == ["a.png", "c.png", "b.png"]
    # Auto watch: Off before On ascending
    bakes = [srow("/x/a.png", bake=True), srow("/x/b.png"), srow("/x/c.png", bake=True)]
    assert names(app_mod.sort_rows(bakes, "bake", False)) == ["b.png", "a.png", "c.png"]
    assert names(app_mod.sort_rows(bakes, "bake", True)) == ["a.png", "c.png", "b.png"]  # ties still by name
    # Status: problems first, then name
    st = [
        srow("/x/1.png"),
        srow("/x/2.png", status=core.NEEDS_CONVERTING, busy=True),
        srow("/x/3.png", status=core.IN_USE),
        srow("/x/4.png", status=core.NEEDS_CONVERTING),
        srow("/x/5.png", status=core.NEEDS_TOOL),
        srow("/x/6.png", status=core.FLAGGED),
        srow("/x/7.png", status=core.MISSING),
        srow("/x/8.png", status="error"),
    ]
    assert names(app_mod.sort_rows(st, "status", False)) == [f"{n}.png" for n in (8, 7, 6, 5, 4, 3, 2, 1)]
    # same column value in two folders: folder breaks the tie after the name
    same = [srow("/b/a.png"), srow("/a/a.png")]
    assert [os.path.basename(r.folder) for r in app_mod.sort_rows(same, "file", False)] == ["a", "b"]


def test_default_sort_header_click_and_arrows(tmp_path, tree):
    app = make_app(tmp_path)
    app.add_files([tree / "b.png", tree / "odd.png", tree / "a.png", tree / "other" / "c.png"])
    assert names(app.rows) == ["a.png", "b.png", "c.png", "odd.png"]  # default: File ascending
    assert app.sort_text() == "File asc"
    assert [app.header_text(c) for c in ("bake", "file", "size")] == ["Auto watch", "File ▲", "Size"]
    app.sort_by("file")  # same column again reverses
    assert names(app.rows) == ["odd.png", "c.png", "b.png", "a.png"] and app.header_text("file") == "File ▼"
    app.sort_by("Size")  # another column starts ascending
    assert app.sort_text() == "Size asc" and app.rows[0].name == "odd.png"  # 60x40 is the fewest pixels
    assert app.header_text("file") == "File" and app.header_text("size") == "Size ▲"
    app.sort_by("max size", True)
    assert app.sort_text() == "Max size desc"
    app.sort_by("nonsense")
    assert "unknown column" in app.message and app.sort_text() == "Max size desc"
    app.close()


def test_sort_is_applied_on_drop_remove_start_not_on_status_change(tmp_path, tree, monkeypatch):
    app = make_app(tmp_path)
    app.add_files([tree / "a.png", tree / "b.png", tree / "odd.png"])
    app.sort_by("status")  # flagged (odd) first, then the two that need converting
    first = [r.key for r in app.rows]
    assert names(app.rows) == ["odd.png", "a.png", "b.png"]
    monkeypatch.setattr(core, "convert", fake_convert(fail=("b.png",)))
    app.convert_all()
    wait_for(app, lambda: not app.converting)
    assert by_name(app)["a.png"].status == core.UP_TO_DATE and by_name(app)["b.png"].status == "error"
    assert [r.key for r in app.rows] == first  # statuses changed but no row moved
    app.sort_by("status", False)  # the header click applies the new statuses
    assert names(app.rows) == ["b.png", "odd.png", "a.png"]  # error, flagged, up to date
    app.close()


def test_selection_survives_resort(tmp_path, tree):
    app = make_app(tmp_path)
    app.add_files([tree / "a.png", tree / "b.png"])
    key = app.rows[0].key
    app.selection = [key]
    app.sort_by("file", True)
    assert app.selection == [key] and app.rows[-1].key == key
    app.close()
