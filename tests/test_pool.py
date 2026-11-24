"""Tests for loki.pool.ConvertPool and the worker limit: fake slow convert_fn, bounded waits, no real tool."""

import os
import threading
import time
from pathlib import Path

from loki import core
from loki.pool import ConvertPool


class Probe:
    """convert_fn that records concurrency, per-key overlap and call order."""

    def __init__(self, delay=0.05, hold: threading.Event | None = None):
        self.delay, self.hold = delay, hold
        self.lock = threading.Lock()
        self.now = self.peak = 0
        self.calls: list[str] = []
        self.active: set[str] = set()
        self.overlap = False

    def __call__(self, png, max_size):
        name = Path(png).name
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            self.calls.append(name)
            self.overlap = self.overlap or name in self.active
            self.active.add(name)
        if self.hold:
            self.hold.wait(5)
        time.sleep(self.delay)
        with self.lock:
            self.now -= 1
            self.active.discard(name)
        return core.ConvertResult(Path(png), Path(png).with_suffix(".paa"), True, None)


def wait_idle(pool, timeout=10.0):
    end = time.monotonic() + timeout
    while not pool.idle():
        assert time.monotonic() < end, "pool never went idle"
        time.sleep(0.005)


def wait_started(probe, timeout=5.0):
    end = time.monotonic() + timeout
    while not probe.calls:
        assert time.monotonic() < end, "nothing started"
        time.sleep(0.005)


def test_convert_workers_is_min_16_of_cpu_minus_4_and_at_least_1():
    assert core.convert_workers(20) == 16
    assert core.convert_workers(24) == 16  # capped
    assert core.convert_workers(8) == 4
    assert core.convert_workers(5) == 1
    assert core.convert_workers(4) == 1 and core.convert_workers(1) == 1  # never below one
    assert core.convert_workers(None) == core.convert_workers(os.cpu_count())
    assert ConvertPool(lambda p, m: None).workers == core.convert_workers()


def test_peak_concurrency_equals_the_limit_and_never_above():
    probe = Probe(delay=0.05)
    pool = ConvertPool(probe, workers=3)
    for i in range(12):
        pool.submit(f"k{i}", Path(f"f{i}.png"), None, "all")
    wait_idle(pool)
    assert probe.peak == 3 and len(probe.calls) == 12
    pool.shutdown()


def test_submit_while_running_queues_exactly_one_followup_and_never_overlaps():
    hold = threading.Event()
    probe = Probe(delay=0.01, hold=hold)
    results = []
    pool = ConvertPool(probe, workers=4, on_result=lambda k, t, r: results.append((k, t)))
    pool.submit("k", Path("a.png"), None, "bake")
    wait_started(probe)
    for tag in ("bake", "bake", "all"):  # three saves during the run collapse into one follow-up
        pool.submit("k", Path("a.png"), None, tag)
    assert pool.busy_keys() == {"k"}
    hold.set()
    wait_idle(pool)
    assert probe.calls == ["a.png", "a.png"] and not probe.overlap
    assert results == [("k", frozenset({"bake"})), ("k", frozenset({"bake", "all"}))]  # follow-up merges tags
    pool.shutdown()


def test_resubmitting_a_queued_key_updates_the_job_in_place():
    hold = threading.Event()
    probe = Probe(delay=0, hold=hold)
    pool = ConvertPool(probe, workers=1)
    pool.submit("busy", Path("busy.png"), None, "all")
    wait_started(probe)
    pool.submit("q", Path("old.png"), None, "all")
    pool.submit("q", Path("new.png"), 256, "bake")
    hold.set()
    wait_idle(pool)
    assert probe.calls == ["busy.png", "new.png"]
    pool.shutdown()


def test_cancel_drops_not_started_jobs_and_followups_but_not_running_ones():
    hold = threading.Event()
    probe = Probe(delay=0, hold=hold)
    pool = ConvertPool(probe, workers=1)
    pool.submit("run", Path("run.png"), None, "all")
    wait_started(probe)
    pool.submit("run", Path("run.png"), None, "bake")  # follow-up
    pool.submit("wait", Path("wait.png"), None, "all")  # queued, not started
    assert sorted(pool.cancel(["run", "wait", "ghost"])) == ["run", "wait"]
    hold.set()
    wait_idle(pool)
    assert probe.calls == ["run.png"]  # the running one finished, nothing else ran
    pool.shutdown()


def test_a_raising_convert_fn_is_a_failed_result_and_the_worker_survives():
    got = []

    def fn(png, cap):
        if Path(png).name == "bad.png":
            raise RuntimeError("kaboom")
        return core.ConvertResult(Path(png), Path(png).with_suffix(".paa"), True, None)

    pool = ConvertPool(fn, workers=1, on_result=lambda k, t, r: got.append((k, r.ok, r.error)))
    pool.submit("a", Path("bad.png"), None, "all")
    pool.submit("b", Path("good.png"), None, "all")
    wait_idle(pool)
    assert got == [("a", False, "RuntimeError: kaboom"), ("b", True, None)]
    pool.shutdown()


def test_key_is_not_busy_when_on_result_runs_but_idle_waits_for_it():
    seen = []
    release = threading.Event()
    pool_box = []

    def on_result(key, tags, res):
        seen.append((key in pool_box[0].busy_keys(), pool_box[0].idle()))
        release.wait(5)

    pool = ConvertPool(Probe(delay=0), workers=1, on_result=on_result)
    pool_box.append(pool)
    pool.submit("k", Path("a.png"), None, "all")
    end = time.monotonic() + 5
    while not seen:
        assert time.monotonic() < end
        time.sleep(0.005)
    assert seen == [(False, False)]  # key free for a rebuild, yet the pool is not idle yet
    assert pool.busy_keys() == set() and not pool.idle()
    release.set()
    wait_idle(pool)
    pool.shutdown()


def test_bake_submit_on_a_queued_all_job_keeps_all():
    hold = threading.Event()
    probe = Probe(delay=0, hold=hold)
    results = []
    pool = ConvertPool(probe, workers=1, on_result=lambda k, t, r: results.append((k, t)))
    pool.submit("busy", Path("busy.png"), None, "bake")
    wait_started(probe)
    pool.submit("q", Path("q.png"), None, "all")
    pool.submit("q", Path("q.png"), None, "bake")
    hold.set()
    wait_idle(pool)
    assert results[-1] == ("q", frozenset({"all", "bake"}))
    pool.shutdown()
