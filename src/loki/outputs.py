"""Loki's own record of the output settings it used (#60, decision 55): %APPDATA%\\Loki\\outputs.json.

The PAA carries no tag for it (shipped files hold only the standard tags), so "was this .paa made with these
--size settings?" is answered from here. One entry per .paa:

    {"version": 1, "entries": {"<file_key of the .paa>": {"size": <bytes>, "mtime_ns": <int>, "settings": "<key>"}}}

An entry counts only while the .paa's current byte size and mtime_ns equal the recorded ones: a file replaced
by anything else (another tool, a plain run, the user) no longer matches and is reconverted. A missing, corrupt
or unreadable store is an empty one. Writes are atomic (temp file in the same folder, then os.replace) and
serialised by one in-process lock plus a cross-process lock file (`outputs.json.lock`, its own file), so the
parallel converters (--jobs, the window's pool) and several Loki processes do not lose entries. Every write
prunes entries whose .paa is gone. The path comes from `store_path` (env LOKI_OUTPUTS overrides it, for tests).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

VERSION = 1
ENV_PATH = "LOKI_OUTPUTS"
_LOCK_WAIT = 30.0  # seconds to wait for another process before giving up on a write
_thread_lock = threading.Lock()


def store_path() -> Path:
    """The store file: $LOKI_OUTPUTS, else %APPDATA%/Loki/outputs.json (next to settings.json)."""
    env = os.environ.get(ENV_PATH)
    if env:
        return Path(env)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Loki" / "outputs.json"


def _key(paa: str | Path) -> str:
    return os.path.normcase(os.path.abspath(paa))  # the same rule as core.file_key


def _sig(paa: str | Path) -> tuple[int, int] | None:
    try:
        st = os.stat(paa)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _load(path: Path) -> dict:
    """The entries of the store; {} when it is missing, unreadable or not what we wrote."""
    try:
        data = json.loads(path.read_text(encoding="utf8"))
        entries = data["entries"]
        if data.get("version") != VERSION or not isinstance(entries, dict):
            return {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


@contextlib.contextmanager
def _locked(path: Path):
    """Exclusive access for a read-modify-write: the in-process lock, then a byte lock on the lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, open(path.with_name(path.name + ".lock"), "a+b") as fh:
        deadline = time.monotonic() + _LOCK_WAIT
        while True:
            try:
                _lock_file(fh)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                _unlock_file(fh)


if os.name == "nt":
    import msvcrt

    def _lock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _write(path: Path, entries: dict) -> None:
    """Atomic: the whole file goes to a temp file in the same folder, then replaces the store."""
    entries = {k: v for k, v in entries.items() if os.path.exists(k)}  # prune entries whose .paa is gone
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps({"version": VERSION, "entries": entries}, indent=1), encoding="utf8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def remember(paa: str | Path, settings: str) -> None:
    """Record that Loki wrote `paa` (as it is on disk now) with `settings`. A store that cannot be written is
    not an error: the file is then simply reconverted next time."""
    sig = _sig(paa)
    if sig is None:
        return
    path = store_path()
    try:
        with _locked(path):
            entries = _load(path)
            entries[_key(paa)] = {"size": sig[0], "mtime_ns": sig[1], "settings": settings}
            _write(path, entries)
    except OSError:
        pass


def forget(paa: str | Path) -> None:
    """Drop the record of `paa` (a plain run rewrote it). Writes nothing when there is none."""
    path = store_path()
    if _key(paa) not in _load(path):
        return
    try:
        with _locked(path):
            entries = _load(path)
            if entries.pop(_key(paa), None) is not None:
                _write(path, entries)
    except OSError:
        pass


def recorded(paa: str | Path) -> str | None:
    """The settings Loki recorded for `paa`, only while its size and mtime still match; else None."""
    entry = _load(store_path()).get(_key(paa))
    sig = _sig(paa)
    if entry is None or sig is None:
        return None
    if entry.get("size") != sig[0] or entry.get("mtime_ns") != sig[1]:
        return None
    settings = entry.get("settings")
    return settings if isinstance(settings, str) else None
