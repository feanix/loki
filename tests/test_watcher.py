"""Tests for loki.watcher over the listed files: fake clock, recorder convert_fn, no real tool, no sleeps
(except one bounded real-time smoke test)."""

import os
import threading
import time
from pathlib import Path

from PIL import Image

from loki import core
from loki.pool import ConvertPool
from loki.settings import Settings
from loki.watcher import Watcher
from paa_helpers import paa_bytes, paa_for_png


def mk(path: Path, size=(64, 64), color=(10, 20, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def set_mtime(path: Path, t: float):
    os.utime(path, (t, t))


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Recorder:
    """convert_fn stand-in: writes a .paa like the tool would, and records (png, cap) per call."""

    def __init__(self):
        self.calls: list[tuple[Path, int | None]] = []

    def __call__(self, png: Path, max_size: int | None):
        self.calls.append((Path(png), max_size))
        paa = Path(png).with_suffix(".paa")
        paa.write_bytes(paa_for_png(Path(png), max_size))
        return core.ConvertResult(Path(png), paa, True, None)

    @property
    def pngs(self):
        return [p for p, _ in self.calls]


def sync_submit(rec):
    """Watcher submit stand-in that converts inline through the recorder (the pool has its own tests)."""
    return lambda key, png, max_size, tag: rec(png, max_size)


def make(tmp_path, settle=1.0):
    folder = tmp_path / "tex"
    folder.mkdir(exist_ok=True)
    s = Settings(path=tmp_path / "cfg" / "settings.json")
    clock, rec = Clock(), Recorder()
    w = Watcher(s, sync_submit(rec), settle=settle, clock=clock)
    return folder, s, clock, rec, w


def listed(s, path, bake=False, cap=None):
    """Put a file on the list (as a drop does), optionally with bake on and a cap. Returns its key."""
    (key,) = s.add_files([path])
    if bake:
        s.set_bake([key], True)
    if cap:
        s.set_max_size([key], cap)
    return key


def settle_passes(w, clock, n=3, step=2.0):
    out = []
    for _ in range(n):
        clock.t += step
        out += w.poll_once()
    return out


# --- debounce, one convert per save ---
def test_rename_save_converts_exactly_once(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    png = mk(folder / "a.png")
    set_mtime(png, 1000)
    listed(s, png, bake=True)
    w.poll_once()  # baseline
    tmp = folder / "a.png.tmp"
    mk(tmp, color=(99, 0, 0))
    w.poll_once()
    os.replace(tmp, png)
    set_mtime(png, 2000)
    clock.t += 0.2
    assert w.poll_once() == []  # seen, not settled
    assert rec.calls == []
    settle_passes(w, clock)
    assert rec.pngs == [png.absolute()]
    assert len(list(folder.glob("*.paa"))) == 1
    settle_passes(w, clock)
    assert len(rec.calls) == 1


def test_in_place_multichunk_write_converts_once_after_it_ends(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    png = mk(folder / "a.png")
    listed(s, png, bake=True)
    w.poll_once()
    full = png.read_bytes()
    cut = len(full) // 3
    for i, chunk_end in enumerate((cut, 2 * cut, len(full))):
        png.write_bytes(full[:chunk_end])  # truncated png is unreadable mid-write
        set_mtime(png, 3000 + i)
        clock.t += 0.4  # less than settle between chunks
        w.poll_once()
    assert rec.calls == []
    settle_passes(w, clock)
    assert len(rec.calls) == 1


def test_bake_off_never_writes(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    png = mk(folder / "a.png")
    listed(s, png)
    w.poll_once()
    mk(png, color=(1, 2, 3))
    set_mtime(png, 5000)
    settle_passes(w, clock)
    assert rec.calls == [] and list(folder.glob("*.paa")) == []


def test_toggle_read_each_pass_and_late_toggle_does_not_bake_old_save(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    png = mk(folder / "a.png")
    key = listed(s, png)
    w.poll_once()
    mk(png, color=(1, 2, 3))
    set_mtime(png, 5000)
    settle_passes(w, clock)
    s.set_bake([key], True)
    settle_passes(w, clock)
    assert rec.calls == []  # that save was consumed while off
    mk(png, color=(4, 5, 6))
    set_mtime(png, 6000)
    settle_passes(w, clock)
    assert len(rec.calls) == 1


def test_start_does_not_convert_existing_files(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    a, b = mk(folder / "a.png"), mk(folder / "b.png")
    folder.joinpath("b.paa").write_bytes(b"x")
    set_mtime(b, 10)
    set_mtime(folder / "b.paa", 20)
    listed(s, a, bake=True)
    listed(s, b, bake=True)
    settle_passes(w, clock)
    assert rec.calls == []


# --- adding and removing while running ---
def test_file_added_while_running_is_baselined_not_converted(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    first = mk(folder / "first.png")
    listed(s, first, bake=True)
    settle_passes(w, clock)
    late = mk(folder / "late.png")  # arrives later, already newer than any paa
    key = listed(s, late, bake=True)
    assert not w.is_baselined(key)
    settle_passes(w, clock)
    assert w.is_baselined(key)
    assert rec.calls == [] and list(folder.glob("*.paa")) == []  # adding wrote nothing
    mk(late, color=(7, 7, 7))
    set_mtime(late, 7000)
    settle_passes(w, clock)
    assert rec.pngs == [late.absolute()]  # a real save after the baseline converts


def test_removed_file_is_no_longer_watched_and_is_baselined_again_on_return(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    png = mk(folder / "a.png")
    key = listed(s, png, bake=True)
    w.poll_once()
    s.remove_files([key])
    mk(png, color=(3, 3, 3))
    set_mtime(png, 8000)
    settle_passes(w, clock)
    assert rec.calls == [] and w.entries == [] and not w.is_baselined(key)
    mk(png, color=(4, 4, 4))
    set_mtime(png, 9000)
    listed(s, png, bake=True)  # back on the list with a newer file: a new first sighting
    settle_passes(w, clock)
    assert rec.calls == []


# --- per-row cap ---
def test_each_row_converts_at_its_own_cap_and_edits_apply_next_pass(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    off = mk(folder / "off.png", (512, 256))
    capped = mk(folder / "capped.png", (512, 256))
    k_off = listed(s, off, bake=True)
    k_cap = listed(s, capped, bake=True, cap=256)
    w.poll_once()
    for p in (off, capped):
        mk(p, (512, 256), color=(5, 5, 5))
        set_mtime(p, 4000)
    settle_passes(w, clock)
    assert sorted((p.name, c) for p, c in rec.calls) == [("capped.png", 256), ("off.png", None)]
    assert core.paa_size(folder / "off.paa") == (512, 256)
    assert core.paa_size(folder / "capped.paa") == (256, 128)
    assert {e.status for e in w.entries} == {core.UP_TO_DATE}
    s.set_max_size([k_off], 256)  # only the first row; picked up by the next pass
    w.poll_once()
    by_name = {e.path.name: e for e in w.entries}
    assert by_name["off.png"].status == core.NEEDS_CONVERTING
    assert by_name["off.png"].reason == "PAA is 512x256, max size wants 256x128"
    assert by_name["capped.png"].status == core.UP_TO_DATE
    assert k_cap != k_off


# --- flagged, missing, vanish/return ---
def test_flagged_not_converted_until_fixed_and_resaved(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    png = mk(folder / "a.png")
    listed(s, png, bake=True)
    w.poll_once()
    mk(png, size=(100, 64))
    settle_passes(w, clock)
    assert rec.calls == []
    entry = w.entries[0]
    assert entry.status == core.FLAGGED and "powers of two" in entry.reason
    mk(png, size=(128, 64))
    set_mtime(png, 9000)
    settle_passes(w, clock)
    assert len(rec.calls) == 1
    assert w.entries[0].status == core.UP_TO_DATE


def test_listed_file_missing_at_start_shows_missing_then_recovers(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    png = folder / "later.png"
    listed(s, png, bake=True)
    seen = []
    w.on_change = lambda entries, results: seen.append([(e.path.name, e.status) for e in entries])
    settle_passes(w, clock)
    assert seen[-1] == [("later.png", core.MISSING)] and rec.calls == []
    mk(png)
    settle_passes(w, clock)
    assert seen[-1] == [("later.png", core.UP_TO_DATE)]
    assert rec.pngs == [png.absolute()]  # it came back: counts as a save


def test_vanish_and_return_counts_as_a_new_save_and_paa_is_left_alone(tmp_path):
    folder, s, clock, rec, w = make(tmp_path)
    png = mk(folder / "a.png")
    (folder / "a.paa").write_bytes(b"keep")
    listed(s, png, bake=True)
    w.poll_once()
    png.unlink()
    settle_passes(w, clock)
    assert [e.status for e in w.entries] == [core.MISSING]
    assert (folder / "a.paa").read_bytes() == b"keep" and rec.calls == []
    mk(png)
    settle_passes(w, clock)
    assert len(rec.calls) == 1 and w.entries[0].status == core.UP_TO_DATE


def test_on_change_gets_entries_in_list_order(tmp_path):
    folder, s, _clock, _rec, w = make(tmp_path)
    for name in ("z.png", "a.png", "m.png"):
        listed(s, mk(folder / name))
    seen = []
    w.on_change = lambda entries, results: seen.append([e.path.name for e in entries])
    w.poll_once()
    w.poll_once()
    assert seen == [["z.png", "a.png", "m.png"]]  # once: nothing changed on the second pass


# --- thread behaviour ---
def test_pass_exception_does_not_kill_thread(tmp_path, monkeypatch):
    folder, s, _c, _r, w = make(tmp_path)
    listed(s, mk(folder / "a.png"))
    w.interval = 0.01
    n = {"c": 0}

    def boom(*a, **k):
        n["c"] += 1
        raise RuntimeError("check failed")

    monkeypatch.setattr(core, "check_png", boom)
    w.start()
    deadline = time.monotonic() + 5
    while n["c"] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    w.stop()
    assert n["c"] >= 3 and "check failed" in w.last_error


def test_realtime_smoke_rename_save(tmp_path):
    folder = tmp_path / "tex"
    folder.mkdir()
    png = mk(folder / "a.png")
    s = Settings(path=tmp_path / "s.json")
    listed(s, png, bake=True)
    rec = Recorder()
    w = Watcher(s, sync_submit(rec), interval=0.05, settle=0.2)
    w.start()
    try:
        time.sleep(0.2)  # let the baseline pass run
        mk(folder / "a.png.tmp", color=(9, 9, 9))
        os.replace(folder / "a.png.tmp", png)
        deadline = time.monotonic() + 5
        while not rec.calls and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)  # a duplicate convert would show up here
    finally:
        w.stop()
    assert rec.pngs == [png.absolute()]


def test_status_uses_the_rows_cap(tmp_path):
    folder, s, _clock, _rec, w = make(tmp_path)
    png = mk(folder / "a.png", (512, 256))
    (folder / "a.paa").write_bytes(paa_bytes(512, 256))
    set_mtime(png, 1000)
    set_mtime(folder / "a.paa", 2000)
    listed(s, png, cap=256)
    w.poll_once()
    assert w.entries[0].status == core.NEEDS_CONVERTING
    assert w.entries[0].reason == "PAA is 512x256, max size wants 256x128"


# --- bake goes through the pool ---
def test_files_saved_together_convert_concurrently_through_the_pool(tmp_path):
    folder = tmp_path / "tex"
    folder.mkdir()
    s = Settings(path=tmp_path / "s.json")
    pngs = [mk(folder / f"f{i}.png") for i in range(3)]
    for p in pngs:
        listed(s, p, bake=True)
    gate = threading.Barrier(3, timeout=5)  # all three conversions must be in flight at the same moment
    rec = Recorder()

    def slow(png, cap):
        gate.wait()
        return rec(png, cap)

    pool = ConvertPool(slow, workers=3)
    clock = Clock()
    w = Watcher(s, pool.submit, settle=1.0, clock=clock)
    w.poll_once()
    for p in pngs:
        mk(p, color=(9, 9, 9))
        set_mtime(p, 5000)
    submitted = settle_passes(w, clock)
    assert len(submitted) == 3
    deadline = time.monotonic() + 5
    while not pool.idle() and time.monotonic() < deadline:
        time.sleep(0.01)
    pool.shutdown()
    assert sorted(p.name for p in rec.pngs) == ["f0.png", "f1.png", "f2.png"]  # the barrier proves overlap
