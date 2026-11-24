"""Locked files (#015): a png held open by another program at settle time (or during convert) is retried
and converted once it is readable, never consumed, never converted twice, never flagged.

Watcher tests use a fake clock and real share-mode-0 locks (tests/lockhelp.py); the pool is the real pool.
"""

import contextlib
import os
import random
import time
from itertools import pairwise
from pathlib import Path

from PIL import Image

from lockhelp import Lock, locked, windows_only
from loki import app as app_mod
from loki import core
from loki.pool import ConvertPool
from loki.settings import Settings
from loki.watcher import Watcher
from paa_helpers import paa_for_png

pytestmark = windows_only


def mk(path: Path, color=(10, 20, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color).save(path, format="PNG")
    return path


def resave(path: Path, t: int | None = None, color=(99, 1, 1)):
    mk(path, color)
    if t is not None:
        os.utime(path, (t, t))


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Recorder:
    """convert_fn stand-in: reads the png like the tool does (so a lock fails it), writes a .paa."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, png, max_size):
        png = Path(png)
        try:
            data = png.read_bytes()
        except OSError as exc:
            return core.ConvertResult(png, png.with_suffix(".paa"), False, str(exc), retry=core.is_lock_error(exc))
        assert data
        self.calls.append(png.name)
        png.with_suffix(".paa").write_bytes(paa_for_png(png, max_size))
        return core.ConvertResult(png, png.with_suffix(".paa"), True, None)


def setup(tmp_path, n=1, retry_fast=10.0, retry_slow=5.0, workers=4):
    folder = tmp_path / "tex"
    s = Settings(path=tmp_path / "cfg" / "s.json")
    pngs = [mk(folder / f"t{i}.png") for i in range(n)]
    keys = s.add_files(pngs)
    s.set_bake(keys, True)
    rec = Recorder()
    pool = ConvertPool(rec, workers=workers)
    clock = Clock()
    w = Watcher(s, pool.submit, settle=1.0, clock=clock, retry_fast=retry_fast, retry_slow=retry_slow)
    w.poll_once()  # baseline
    return pngs, keys, s, rec, pool, clock, w


def passes(w, clock, seconds, step=0.5, until=None):
    """Advance the fake clock in `step` passes for `seconds`; stop early when `until()` is true."""
    end = clock.t + seconds
    while clock.t < end:
        clock.t += step
        w.poll_once()
        if until and until():
            break


def drain(pool, timeout=5.0):
    end = time.monotonic() + timeout
    while not pool.idle() and time.monotonic() < end:
        time.sleep(0.005)
    assert pool.idle()


# --- criteria 1 and 4: the advisor case, watcher + pool ---
def test_rewrite_then_lock_past_settle_converts_once_after_release(tmp_path):
    pngs, _keys, _s, rec, pool, clock, w = setup(tmp_path, n=2)
    for i, p in enumerate(pngs):
        resave(p, 5000 + i)
    with locked(pngs[1]):  # t1 stays locked past settle; t0 is free
        passes(w, clock, 4)
        drain(pool)
        assert rec.calls == ["t0.png"]
        assert {e.path.name: e.status for e in w.entries}["t1.png"] == core.IN_USE
    passes(w, clock, 2)
    drain(pool)
    passes(w, clock, 10)  # nothing more may happen
    drain(pool)
    assert sorted(rec.calls) == ["t0.png", "t1.png"]  # each exactly once
    assert {e.status for e in w.entries} == {core.UP_TO_DATE}
    pool.shutdown()


def test_newer_save_supersedes_a_pending_retry(tmp_path):
    pngs, keys, _s, rec, pool, clock, w = setup(tmp_path)
    resave(pngs[0], 5000)
    lock = Lock(pngs[0])
    try:
        passes(w, clock, 3)
        assert w.attempts(keys[0])
    finally:
        lock.release()
    resave(pngs[0], 6000, color=(5, 5, 5))  # a newer save lands before the retry fires
    passes(w, clock, 4)
    drain(pool)
    passes(w, clock, 10)
    drain(pool)
    assert rec.calls == ["t0.png"]
    assert w.attempts(keys[0]) == []
    pool.shutdown()


# --- criterion 2: long lock is quiet and backs off ---
def test_long_lock_backs_off_and_converts_once_on_release(tmp_path):
    pngs, keys, _s, rec, pool, clock, w = setup(tmp_path)
    resave(pngs[0], 5000)
    with locked(pngs[0]):
        passes(w, clock, 90)  # 180 passes at 0.5 s
        times = w.attempts(keys[0])
        assert w.entries[0].status == core.IN_USE and w.entries[0].reason == "file in use"
        assert rec.calls == []
    t0 = times[0]
    fast = [t for t in times if t - t0 < 10.0]
    slow = [t for t in times if t - t0 >= 10.0]
    assert len(fast) >= 19  # every pass for the first 10 s (0.5 s apart)
    assert all(b - a >= 5.0 for a, b in pairwise(slow))  # then at most every 5 s
    assert slow[0] - fast[-1] <= 5.5 and len(times) < 40  # nowhere near 180
    passes(w, clock, 6)  # the next due check sees it free
    drain(pool)
    passes(w, clock, 10)
    drain(pool)
    assert rec.calls == ["t0.png"]
    pool.shutdown()


def test_in_use_is_never_flagged_and_bake_off_consumes_it(tmp_path):
    pngs, keys, s, rec, pool, clock, w = setup(tmp_path)
    s.set_bake(keys, False)
    resave(pngs[0], 5000)
    with locked(pngs[0]):
        passes(w, clock, 4)
        assert w.entries[0].status == core.IN_USE and w.attempts(keys[0]) == []
    passes(w, clock, 4)
    drain(pool)
    assert rec.calls == []  # a bake-off save is consumed, locked or not
    pool.shutdown()


def test_check_png_in_use_vs_bad_data(tmp_path):
    png = mk(tmp_path / "a.png")
    with locked(png):
        e = core.check_png(png)
    assert (e.status, e.reason) == (core.IN_USE, "file in use")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    assert core.check_png(bad).status == core.FLAGGED
    assert not core.is_lock_error(FileNotFoundError()) and core.is_lock_error(PermissionError())
    assert core.is_lock_error(OSError(0, "x", None, 32)) and not core.is_lock_error(OSError(5, "x"))


def test_convert_on_a_locked_png_asks_for_a_retry(tmp_path):
    png = mk(tmp_path / "a.png")
    with locked(png):
        res = core.convert(png, tmp_path / "tool.exe")
    assert not res.ok and res.retry
    missing = core.convert(tmp_path / "gone.png", tmp_path / "tool.exe")
    assert not missing.ok and not missing.retry


# --- criterion 5: bulk, seeded half locked 2-6 s (fake clock, real locks) ---
def test_bulk_twenty_rewritten_half_locked_all_converted_once(tmp_path):
    pngs, _keys, _s, rec, pool, clock, w = setup(tmp_path, n=20)
    rng = random.Random(15)
    for i, p in enumerate(pngs):
        resave(p, 5000 + i)
    held = {p: (Lock(p), clock.t + rng.uniform(2.0, 6.0)) for p in rng.sample(pngs, 10)}
    try:
        end = clock.t + 40
        while clock.t < end:
            clock.t += 0.5
            for p, (lock, until) in held.items():
                if clock.t >= until:
                    lock.release()
            w.poll_once()
            if len(rec.calls) == 20:
                break
            time.sleep(0.002)  # let pool threads run
        drain(pool)
        passes(w, clock, 15)  # a duplicate would show here
        drain(pool)
    finally:
        for lock, _ in held.values():
            lock.release()
    assert sorted(rec.calls) == sorted(p.name for p in pngs)
    assert {e.status for e in w.entries} == {core.UP_TO_DATE}
    pool.shutdown()


# --- criteria 2 and 4 through App (pool path, real threads) ---
def wait(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        app.pump()
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)
    app.pump()


def make_app(tmp_path):
    return app_mod.App(tmp_path / "settings.json", find_tool=lambda _o: Path("fake.exe"), interval=0.02, settle=0.1)


def fake_run_tool(tool, src, dst):
    dst.write_bytes(paa_for_png(Path(src), None))


def test_app_locked_at_settle_waits_then_converts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_tool", fake_run_tool)
    png = mk(tmp_path / "tex" / "a.png")
    app = make_app(tmp_path)
    try:
        app.add_files([png])
        wait(app, app.watching)
        app.set_all(True)
        resave(png, None)
        with locked(png):
            wait(app, lambda: app.rows[0].status == core.IN_USE)
            time.sleep(0.4)  # well past settle: still waiting, not flagged, not converted
            app.pump()
            row = app.rows[0]
            assert row.status == core.IN_USE and row.status_text == "Waiting: file in use"
            assert "in_use: file in use" in app.digest() and "flagged" not in app.digest()
            assert not png.with_suffix(".paa").exists()
        wait(app, lambda: app.rows[0].status == core.UP_TO_DATE)
        mtime = png.with_suffix(".paa").stat().st_mtime_ns
        time.sleep(0.4)
        app.pump()
        assert png.with_suffix(".paa").stat().st_mtime_ns == mtime  # one conversion
    finally:
        app.close()


def test_app_lock_during_convert_retries_and_converts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_tool", fake_run_tool)
    real = core.convert
    seen = []

    def convert_under_lock(png, tool, max_size=None, converter=None):
        seen.append(Path(png).name)
        if len(seen) == 1:  # the png was readable at settle; it is locked when convert opens it
            with contextlib.closing(_Holder(png)):
                res = real(png, tool, max_size, converter)
            assert res.retry and not res.ok
            return res
        return real(png, tool, max_size, converter)

    monkeypatch.setattr(core, "convert", convert_under_lock)
    png = mk(tmp_path / "tex" / "a.png")
    app = make_app(tmp_path)
    try:
        app.add_files([png])
        wait(app, app.watching)
        app.set_all(True)
        resave(png, None)
        wait(app, lambda: png.with_suffix(".paa").exists() and app.rows[0].status == core.UP_TO_DATE)
        time.sleep(0.4)
        app.pump()
        assert seen == ["a.png", "a.png"]  # locked once, then converted once
        assert app.rows[0].status == core.UP_TO_DATE and "error" not in app.digest()
    finally:
        app.close()


class _Holder:
    def __init__(self, path):
        self.lock = Lock(path)

    def close(self):
        self.lock.release()


def test_convert_all_shows_waiting_and_does_not_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_tool", fake_run_tool)
    a, b = mk(tmp_path / "tex" / "a.png"), mk(tmp_path / "tex" / "b.png")
    app = make_app(tmp_path)
    try:
        app.add_files([a, b])
        wait(app, app.watching)
        real = core.convert

        def convert(png, tool, max_size=None, converter=None):
            if Path(png).name == "a.png":  # a is held by another program during its run
                with locked(png):
                    return real(png, tool, max_size, converter)
            return real(png, tool, max_size, converter)

        monkeypatch.setattr(core, "convert", convert)
        assert app.convert_all()
        wait(app, lambda: not app.converting)
        rows = {r.name: r for r in app.rows}
        assert rows["a.png"].status_text == "Waiting: file in use"
        assert rows["b.png"].status == core.UP_TO_DATE
        assert "1 waiting: file in use" in app.message and "failed" not in app.message
        assert app.idle()
    finally:
        app.close()
