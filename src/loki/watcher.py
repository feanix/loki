"""Polling watcher: bakes a listed png to .paa after it is saved, only if its bake toggle is on.

Stdlib polling (no watchdog) so temp/rename saves and multi-step writes are handled by one rule:
a png is "pending" when its (mtime_ns, size) differs from the last consumed signature, and it is
acted on only after the signature has been unchanged for `settle` seconds. The watched set is the
settings file list, re-read every pass (adds, removes and cap edits apply on the next pass). The
first time a file is seen its signature is baselined, never converted (decision 11: adding a file
writes nothing). A pending png that settles with its toggle off, or flagged, is consumed without
converting, so flipping the toggle later does not retroactively bake an old save; only a new save does.
A listed file that vanishes and returns counts as a new save. A file removed from the list is forgotten.
A save whose png is locked (in_use) when it settles is not consumed: it is re-checked with backoff and
submitted once readable (#015); `retry` does the same for a submitted save whose convert hit a lock.
The watcher never converts itself: a settled save is handed to `submit` (the conversion pool, decision 13)
and its result comes back to the owner through the pool, not through this class.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from loki import core
from loki.settings import FileSetting, Settings

log = logging.getLogger(__name__)

Sig = tuple[int, int]


RETRY_FAST = 10.0  # seconds after a locked save settled during which every pass checks it again
RETRY_SLOW = 5.0  # after that, at most one check per this many seconds


def backoff_due(since: float, last: float, now: float, fast: float = RETRY_FAST, slow: float = RETRY_SLOW) -> bool:
    """The one backoff schedule (#015), shared with the CLI: a wait that began at `since` is due every pass for
    `fast` seconds, then at most once per `slow` seconds after the check at `last`."""
    return now - since < fast or now - last >= slow


@dataclass
class _Retry:
    """A save whose png was locked (at settle, or during convert): pending until readable."""

    sig: Sig
    since: float  # clock time the wait began (backoff counts from here)
    last: float  # clock time of the latest check
    attempts: list[float]  # clock time of each check made while waiting
    entry: core.PngEntry  # the in_use entry shown while backing off


class Watcher:
    def __init__(
        self,
        settings: Settings,
        submit: Callable[[str, Path, int | None, str], None],
        on_change: Callable[[list[core.PngEntry], list[core.ConvertResult]], None] | None = None,
        interval: float = 0.5,
        settle: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        retry_fast: float = RETRY_FAST,
        retry_slow: float = RETRY_SLOW,
    ):
        self.settings = settings
        self.submit = submit
        self.on_change = on_change
        self.interval = interval
        self.settle = settle
        self.clock = clock
        self.retry_fast = retry_fast
        self.retry_slow = retry_slow
        self._retry: dict[str, _Retry] = {}  # key -> save waiting on a lock
        self._chain: dict[str, tuple[Sig, float]] = {}  # key -> (sig, since) of the retry that was just submitted
        self._lock = threading.Lock()  # poll_once (watcher thread) vs retry() (App's thread)
        self.entries: list[core.PngEntry] = []
        self.last_error: str | None = None
        self._known: set[str] = set()  # keys seen at least once (so already baselined)
        self._consumed: dict[str, Sig] = {}  # key -> signature already handled
        self._seen: dict[str, tuple[Sig, float]] = {}  # key -> (signature, time first seen)
        self._view: list[tuple] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def is_baselined(self, key: str) -> bool:
        """True once a pass has seen this key, so a later save is a real save, not the baseline."""
        return key in self._known

    def unsettled(self) -> list[str]:
        """Keys of listed files whose current save the watcher has not yet handled: not baselined, or the
        file's signature differs from the last consumed one (still settling, or waiting on a lock)."""
        with self._lock:
            out = []
            for f in self.settings.files():
                sig = core.file_sig(Path(f.path))
                if sig is None:
                    continue  # gone: nothing to handle
                if f.key not in self._known or self._consumed.get(f.key) != sig:
                    out.append(f.key)
            return out

    def attempts(self, key: str) -> list[float]:
        """Clock times of the retry checks made for a save waiting on a lock (empty when none is waiting)."""
        with self._lock:
            rt = self._retry.get(key)
            return list(rt.attempts) if rt else []

    def retry(self, key: str, sig: Sig | None, reason: str = "file in use") -> None:
        """A submitted save came back with `retry=True` (locked during convert): un-consume it so a later
        pass submits it again once readable. Ignored when the file has since changed (`sig` is not the
        consumed signature): the newer save supersedes it and is handled on its own."""
        with self._lock:
            if sig is None or self._consumed.get(key) != sig:
                return
            del self._consumed[key]
            now = self.clock()
            png = next((f for f in self.settings.files() if f.key == key), None)
            if png is None:
                return
            e = core.check_png(png.path, png.max_size, png.size)
            held = replace(e, status=core.IN_USE, reason=reason)
            chain = self._chain.get(key)
            since = chain[1] if chain is not None and chain[0] == sig else now
            self._retry[key] = _Retry(sig, since, now, [], held)

    def _due(self, rt: _Retry, now: float) -> bool:
        """Backoff: every pass for `retry_fast` seconds after the save settled, then at most every `retry_slow`."""
        return backoff_due(rt.since, rt.last, now, self.retry_fast, self.retry_slow)

    def _check_all(self, files: list[FileSetting], now: float) -> list[core.PngEntry]:
        out = []
        for f in files:
            rt = self._retry.get(f.key)
            if rt is not None and core.file_sig(Path(f.path)) == rt.sig:
                if not self._due(rt, now):
                    out.append(rt.entry)  # backing off: no read this pass
                    continue
                rt.last = now
                rt.attempts.append(now)
            out.append(core.check_png(f.path, f.max_size, f.size))
        return out

    def poll_once(self) -> list[str]:
        """One pass. Returns the keys handed to `submit` (settled saves with bake on)."""
        with self._lock:
            return self._poll_locked()

    def _poll_locked(self) -> list[str]:
        now = self.clock()
        files = self.settings.files()
        entries = self._check_all(files, now)
        submitted: list[str] = []
        for f, e in zip(files, entries, strict=True):
            key = f.key
            sig = core.file_sig(e.path)
            if sig is None:
                # Gone: forget its signature so its return reads as a new save.
                self._known.add(key)
                self._seen.pop(key, None)
                self._consumed.pop(key, None)
                self._retry.pop(key, None)
                self._chain.pop(key, None)
                continue
            if key not in self._known:
                self._known.add(key)
                self._consumed[key] = sig
                self._seen[key] = (sig, now)
                continue
            prev = self._seen.get(key)
            if prev is None or prev[0] != sig:
                self._seen[key] = (sig, now)
                self._retry.pop(key, None)  # a newer save supersedes a pending retry
                continue  # still changing (or just appeared): wait for it to settle
            rt = self._retry.get(key)
            if self._consumed.get(key) == sig or (rt is None and now - prev[1] < self.settle):
                continue
            if e.status == core.IN_USE:
                if not f.bake:
                    self._consumed[key] = sig  # nothing to convert: a locked save with bake off is just consumed
                    self._retry.pop(key, None)
                elif rt is None:  # locked at settle: keep the save pending, check again with backoff
                    self._retry[key] = _Retry(sig, now, now, [now], e)
                continue
            rt_old = self._retry.pop(key, None)
            if rt_old is not None:  # a retry() for this same save continues the backoff, it does not restart it
                self._chain[key] = (rt_old.sig, rt_old.since)
            self._consumed[key] = sig
            if e.status in (core.FLAGGED, core.MISSING) or not f.bake:
                continue
            self.submit(key, e.path, f.max_size, "bake")
            submitted.append(key)
        listed = {f.key for f in files}
        for key in set(self._known) - listed:
            self._known.discard(key)
            self._seen.pop(key, None)
            self._consumed.pop(key, None)
            self._retry.pop(key, None)
            self._chain.pop(key, None)
        if submitted:
            entries = [core.check_png(f.path, f.max_size, f.size) if f.key in submitted else e for f, e in zip(files, entries, strict=True)]
        view = [(e.path, e.status, e.reason, e.size) for e in entries]
        self.entries = entries
        if submitted or view != self._view:
            self._view = view
            if self.on_change:
                self.on_change(entries, [])
        return submitted

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.last_error = None
            except Exception as exc:  # the thread must survive any pass failure
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("watcher pass failed")
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="loki-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
