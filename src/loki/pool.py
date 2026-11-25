"""The one conversion pool: bounded parallel ImageToPAA runs for Convert all and bake.

Fixed worker threads over one queue, all state under one lock. Rules:
  * at most `workers` convert_fn calls run at once;
  * a key never runs on two workers at once: a submit while it is queued updates that job in place,
    a submit while it is running queues exactly one follow-up for after the run;
  * `cancel` drops jobs that have not started (a running job finishes);
  * a job carries the union of the tags of every submit it absorbed (queued update or follow-up);
  * `on_result(key, tags, result)` is called on the worker thread after each run. The key has left
    the running set by then (a consumer that rebuilds on the result never sees it busy), but the
    job still counts for `idle()` until `on_result` has returned, so idle never precedes the last result.
No Tk, no settings: callers say what to convert and with which cap.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from loki import core

log = logging.getLogger(__name__)


@dataclass
class _Job:
    png: Path
    max_size: int | None
    tags: frozenset[str]


class ConvertPool:
    def __init__(
        self,
        convert_fn: Callable[[Path, int | None], core.ConvertResult],
        workers: int | None = None,
        on_result: Callable[[str, frozenset[str], core.ConvertResult], None] | None = None,
    ):
        self.convert_fn = convert_fn
        self.workers = core.convert_workers() if workers is None else max(1, workers)
        self.on_result = on_result
        self._cond = threading.Condition()
        self._queued: dict[str, _Job] = {}  # insertion order = start order
        self._running: dict[str, _Job] = {}
        self._followup: dict[str, _Job] = {}  # at most one per running key
        self._inflight = 0  # jobs taken by a worker whose on_result has not returned yet
        self._stopping = False
        self._threads: list[threading.Thread] = []

    def submit(self, key: str, png: Path, max_size: int | None, tag: str) -> None:
        with self._cond:
            if self._stopping:
                return
            if key in self._running:
                old = self._followup.get(key)  # latest png/cap wins; exactly one follow-up, tags merged
                self._followup[key] = _Job(Path(png), max_size, (old.tags if old else frozenset()) | {tag})
            else:
                old = self._queued.get(key)  # a queued key is updated in place, keeping its place
                self._queued[key] = _Job(Path(png), max_size, (old.tags if old else frozenset()) | {tag})
                self._spawn_locked()
                self._cond.notify()

    def cancel(self, keys: Iterable[str]) -> list[str]:
        """Drop not-started jobs (and pending follow-ups) for these keys; returns the keys dropped."""
        dropped = []
        with self._cond:
            for key in set(keys):
                hit = self._queued.pop(key, None) is not None
                hit = self._followup.pop(key, None) is not None or hit
                if hit:
                    dropped.append(key)
        return dropped

    def busy_keys(self) -> set[str]:
        with self._cond:
            return set(self._queued) | set(self._running) | set(self._followup)

    def idle(self) -> bool:
        with self._cond:
            return not (self._queued or self._running or self._followup or self._inflight)

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work and drop queued jobs; running ones finish."""
        with self._cond:
            self._stopping = True
            self._queued.clear()
            self._followup.clear()
            self._cond.notify_all()
            threads = list(self._threads)
        if wait:
            for t in threads:
                t.join()

    # --- workers ------------------------------------------------------------------------------

    def _spawn_locked(self) -> None:
        """Start a worker lazily while fewer than `workers` exist (so an unused pool costs nothing)."""
        if len(self._threads) < self.workers and len(self._threads) < len(self._queued) + len(self._running):
            t = threading.Thread(target=self._work, name=f"loki-convert-{len(self._threads)}", daemon=True)
            self._threads.append(t)
            t.start()

    def _take_locked(self) -> tuple[str, _Job] | None:
        for key in self._queued:
            job = self._queued.pop(key)
            self._running[key] = job
            self._inflight += 1
            return key, job
        return None

    def _work(self) -> None:
        while True:
            with self._cond:
                picked = self._take_locked()
                while picked is None:
                    if self._stopping:
                        return
                    self._cond.wait()
                    picked = self._take_locked()
            key, job = picked
            try:
                res = self.convert_fn(job.png, job.max_size)
            except Exception as exc:  # noqa: BLE001 -- a worker must outlive any one failed file
                res = core.ConvertResult(job.png, job.png.with_suffix(".paa"), False, f"{type(exc).__name__}: {exc}")
            with self._cond:
                del self._running[key]  # before on_result: the row it rebuilds must not see this key busy
                nxt = self._followup.pop(key, None)
                if nxt is not None:
                    self._queued[key] = nxt
                    self._spawn_locked()
                    self._cond.notify()
            try:
                if self.on_result:
                    self.on_result(key, job.tags, res)
            except Exception:  # a bad callback must not kill the worker
                log.exception("on_result failed")
            finally:
                with self._cond:
                    self._inflight -= 1
