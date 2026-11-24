"""Output side (#028): the existing .paa is held open by another program (TexView, Arma file patching,
Explorer preview, AV) when the converted file is due to replace it. The save stays pending, the old .paa
stays intact, nothing is left behind, and it is converted once the hold ends. Real CreateFileW holds
(GENERIC_READ, FILE_SHARE_READ); the watcher tests run on a fake clock.
"""

import os
import time
from pathlib import Path

import pytest

from lockhelp import Lock, locked, windows_only
from loki import core
from loki.pool import ConvertPool
from loki.settings import Settings
from loki.watcher import Watcher
from paa_helpers import paa_for_png
from test_locked import Clock, drain, fake_run_tool, make_app, mk, passes, resave, wait

pytestmark = windows_only

FILE_SHARE_READ = 1
OLD = b"old paa bytes"


@pytest.fixture(autouse=True)
def fast_replace(monkeypatch):
    monkeypatch.setattr(core, "_run_tool", fake_run_tool)
    monkeypatch.setattr(core, "_REPLACE_PAUSE", 0.001)  # the short #027 retry, shrunk: a held PAA fails fast


def hold(paa):
    return Lock(paa, share=FILE_SHARE_READ)


def tmp_leftovers(folder):
    return [p for p in folder.iterdir() if p.name.startswith(".loki-tmp-")]


def png_with_old_paa(tmp_path):
    png = mk(tmp_path / "tex" / "a.png")
    paa = png.with_suffix(".paa")
    paa.write_bytes(OLD)
    return png, paa


# --- what Windows does while the PAA is held ---
def test_held_paa_replace_is_winerror_5_and_probe_says_sharing_violation(tmp_path):
    import os

    _png, paa = png_with_old_paa(tmp_path)
    new = tmp_path / "new.paa"
    new.write_bytes(b"new")
    with hold(paa):
        with pytest.raises(PermissionError) as ei:
            os.replace(new, paa)
        assert ei.value.winerror == 5
        assert core._probe_error(paa, 0x00010000) == 32
        assert core._held_open(ei.value, paa)
    assert core._probe_error(paa, 0x00010000) is None
    assert not core._held_open(PermissionError(13, "x"), tmp_path / "gone.paa")


# --- criteria 1 and 3: core.convert ---
def test_convert_on_held_paa_asks_for_retry_and_leaves_nothing(tmp_path):
    png, paa = png_with_old_paa(tmp_path)
    with hold(paa):
        res = core.convert(png, Path("fake.exe"), None, core.BOHEMIA)
        assert not res.ok and res.retry and res.error == core.PAA_IN_USE
        assert paa.read_bytes() == OLD
        assert tmp_leftovers(paa.parent) == []
    res = core.convert(png, Path("fake.exe"), None, core.BOHEMIA)
    assert res.ok and paa.read_bytes() == paa_for_png(png) and tmp_leftovers(paa.parent) == []


def test_loki_converter_path_also_waits_on_held_paa(tmp_path):
    png, paa = png_with_old_paa(tmp_path)
    with hold(paa):
        res = core.convert(png, None, None, core.LOKI)
        assert not res.ok and res.retry and res.error == core.PAA_IN_USE
        assert paa.read_bytes() == OLD and tmp_leftovers(paa.parent) == []
    assert core.convert(png, None, None, core.LOKI).ok


# --- criterion 4: anything that is not a hold stays an error ---
def test_non_lock_failures_are_errors_not_waits(tmp_path, monkeypatch):
    png, paa = png_with_old_paa(tmp_path)

    def acl(_src, _dst):
        raise PermissionError(13, "Access is denied", None, 5)  # winerror 5, nobody holds the file

    monkeypatch.setattr(core, "_replace_retrying", acl)
    res = core.convert(png, Path("fake.exe"), None, core.BOHEMIA)
    assert not res.ok and not res.retry and "PermissionError" in res.error
    assert paa.read_bytes() == OLD and tmp_leftovers(paa.parent) == []

    def full(_src, _dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(core, "_replace_retrying", full)
    res = core.convert(png, Path("fake.exe"), None, core.BOHEMIA)
    assert not res.ok and not res.retry and "No space" in res.error
    assert tmp_leftovers(paa.parent) == []


def test_held_by_a_real_acl_denial_is_not_a_hold(tmp_path):
    _png, paa = png_with_old_paa(tmp_path)
    exc = PermissionError(13, "Access is denied", None, 5)
    assert not core._held_open(exc, paa)  # nobody has it open: the probe opens it


# --- criterion 1: watcher + pool, fake clock, the advisor case at 6 s and 60 s ---
def watched(tmp_path):
    png, paa = png_with_old_paa(tmp_path)
    s = Settings(path=tmp_path / "cfg" / "s.json")
    keys = s.add_files([png])
    s.set_bake(keys, True)
    results = []
    watcher = []

    def convert_fn(p, max_size):
        res = core.convert(p, Path("fake.exe"), max_size, core.BOHEMIA)
        results.append(res)
        if res.retry:  # what App._apply_result does with a bake result that asks for a retry
            watcher[0].retry(core.file_key(p), core.file_sig(Path(p)), res.error)
        return res

    pool = ConvertPool(convert_fn, workers=2)
    clock = Clock()
    w = Watcher(s, pool.submit, settle=1.0, clock=clock)
    watcher.append(w)
    w.poll_once()
    return png, paa, keys, results, pool, clock, w


@pytest.mark.parametrize("hold_seconds", [6, 60])
def test_save_waits_while_paa_held_then_converts_once(tmp_path, hold_seconds):
    png, paa, _keys, results, pool, clock, w = watched(tmp_path)
    resave(png, 5000)
    lock = hold(paa)
    try:
        end = clock.t + hold_seconds
        while clock.t < end:
            passes(w, clock, 0.5)
            drain(pool)
        assert results and all(r.retry and not r.ok for r in results)  # waited, never an error
        assert paa.read_bytes() == OLD and tmp_leftovers(paa.parent) == []
        waiting = len(results)
    finally:
        lock.release()
    passes(w, clock, 12)
    drain(pool)
    passes(w, clock, 12)
    drain(pool)
    assert [r.ok for r in results].count(True) == 1  # one successful conversion
    assert paa.read_bytes() != OLD and tmp_leftovers(paa.parent) == []
    assert waiting < 40  # backed off, not one try per pass
    assert core.check_png(png).status == core.UP_TO_DATE
    pool.shutdown()


# --- criterion 2 and the row text, through App (real pool and threads) ---
def test_app_bake_waits_on_held_paa_row_says_paa_in_use_then_converts(tmp_path):
    png, paa = png_with_old_paa(tmp_path)
    app = make_app(tmp_path)
    try:
        app.add_files([png])
        wait(app, app.watching)
        app.set_all(True)
        resave(png, None)
        with hold(paa):
            wait(app, lambda: app.rows[0].status == core.IN_USE)
            time.sleep(0.4)
            app.pump()
            row = app.rows[0]
            assert row.status == core.IN_USE and row.status_text == "Waiting: PAA in use"
            assert "error" not in app.digest() and paa.read_bytes() == OLD
        wait(app, lambda: app.rows[0].status == core.UP_TO_DATE, timeout=30)
        assert paa.read_bytes() != OLD and tmp_leftovers(paa.parent) == []
    finally:
        app.close()


def test_convert_all_on_held_paa_waits_per_row_not_error(tmp_path):
    a, pa = png_with_old_paa(tmp_path)
    b = mk(tmp_path / "tex" / "b.png")
    app = make_app(tmp_path)
    try:
        app.add_files([a, b])
        wait(app, app.watching)
        with hold(pa):
            assert app.convert_all()
            wait(app, lambda: not app.converting)
            rows = {r.name: r for r in app.rows}
            assert rows["a.png"].status == core.IN_USE and rows["a.png"].status_text == "Waiting: PAA in use"
            assert rows["b.png"].status == core.UP_TO_DATE
            assert "1 waiting" in app.message and "failed" not in app.message and "error" not in app.digest()
            assert pa.read_bytes() == OLD and tmp_leftovers(pa.parent) == []
        assert app.convert_all()  # the user runs it again once the hold is gone
        wait(app, lambda: not app.converting)
        assert {r.name: r.status for r in app.rows} == {"a.png": core.UP_TO_DATE, "b.png": core.UP_TO_DATE}
    finally:
        app.close()


def test_png_lock_still_reads_file_in_use(tmp_path):
    png = mk(tmp_path / "tex" / "a.png")
    app = make_app(tmp_path)
    try:
        app.add_files([png])
        wait(app, app.watching)
        with locked(png):
            assert app.convert_all()
            wait(app, lambda: not app.converting)
            assert app.rows[0].status_text == "Waiting: file in use"
    finally:
        app.close()


@pytest.mark.parametrize(
    "hold_seconds",
    [6, pytest.param(60, marks=pytest.mark.skipif(not os.environ.get("LOKI_SLOW"), reason="real 60 s hold: LOKI_SLOW=1"))],
)
def test_app_real_clock_hold_then_release_updates_paa_once(tmp_path, hold_seconds):
    png, paa = png_with_old_paa(tmp_path)
    app = make_app(tmp_path)
    try:
        app.add_files([png])
        wait(app, app.watching)
        app.set_all(True)
        resave(png, None)
        with hold(paa):
            t_end = time.monotonic() + hold_seconds
            while time.monotonic() < t_end:
                app.pump()
                time.sleep(0.02)
            wait(app, lambda: app.rows[0].status_text == "Waiting: PAA in use")  # not mid-attempt
            assert paa.read_bytes() == OLD
            assert "error" not in app.digest()  # (a temp dir may exist mid-attempt: checked once idle)
        wait(app, lambda: app.rows[0].status == core.UP_TO_DATE and app.idle(), timeout=30)
        assert tmp_leftovers(paa.parent) == []
        mtime = paa.stat().st_mtime_ns
        time.sleep(0.5)
        app.pump()
        assert paa.stat().st_mtime_ns == mtime and paa.read_bytes() != OLD
    finally:
        app.close()
