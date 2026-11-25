"""Persisted settings (JSON) for Loki: the converter choice and the file list with each file's max size,
bake toggle and the converter that wrote its last .paa.

The one place for everything saved between launches. A missing or corrupt file yields defaults and
never raises; writes are atomic (temp file in the same directory, then os.replace) so a crash
mid-save cannot leave a half-written file. A 0.1.0 file (folder model) still loads: its tool path
is kept, its folder fields are ignored and never written back.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from loki import core

VERSION = 2
SORT_COLUMNS = ("bake", "file", "folder", "size", "max", "outsize", "status")  # ids of the sortable columns (app.COLUMNS order)
DEFAULT_SORT: tuple[str, bool] = ("file", False)  # (column id, descending): File, ascending


def default_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Loki" / "settings.json"


@dataclass(frozen=True)
class FileSetting:
    path: str  # absolute
    max_size: int | None = None  # None = Off
    bake: bool = False
    made_by: str | None = None  # core.LOKI / core.BOHEMIA: who wrote the .paa last (None = not by Loki)
    size: core.OutputSize | None = None  # None = Source; replaces max_size when set (0.3.1)

    @property
    def key(self) -> str:
        return core.file_key(self.path)


def _size_to_json(size: core.OutputSize | None) -> dict | None:
    if size is None:
        return None
    return {
        "width": size.width,
        "height": size.height,
        "fit": size.fit,
        "colour": list(size.colour) if size.colour is not None else None,
        "align": size.align,
    }


def _size_from_json(raw: object) -> core.OutputSize | None:
    """An output size from a settings entry; anything absent or invalid is None (Source)."""
    if not isinstance(raw, dict):
        return None
    try:
        colour = raw.get("colour")
        if colour is not None:
            if not (isinstance(colour, list) and len(colour) == 4 and all(
                isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in colour
            )):
                return None
            colour = "{},{},{},{}".format(*colour)
        w, h = raw.get("width"), raw.get("height")
        if not (isinstance(w, int) and isinstance(h, int)) or isinstance(w, bool) or isinstance(h, bool):
            return None
        return core.output_size(f"{w}x{h}", str(raw.get("fit", "contain")), colour, str(raw.get("align", "centre")))
    except ValueError:
        return None


def _valid_cap(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value in core.MAX_SIZES)


@dataclass
class Settings:
    imagetopaa_path: str | None = None
    converter: str = core.LOKI  # the user's choice; a file's route may still need ImageToPAA
    sort: tuple[str, bool] = DEFAULT_SORT  # (column id, descending) of the file list; remembered
    path: Path | None = field(default=None, repr=False, compare=False)
    _files: list[FileSetting] = field(default_factory=list, repr=False, compare=False)
    # The watcher thread reads the list while the GUI thread edits it.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        p = Path(path) if path is not None else default_path()
        s = cls(path=p)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        if not isinstance(data, dict):
            return s
        if isinstance(data.get("imagetopaa_path"), str):
            s.imagetopaa_path = data["imagetopaa_path"]
        if data.get("converter") in core.CONVERTERS:
            s.converter = data["converter"]
        stored = data.get("sort")  # absent in 0.3.0/0.3.1-early files; anything unknown stays File ascending
        if (
            isinstance(stored, dict)
            and stored.get("column") in SORT_COLUMNS
            and isinstance(stored.get("descending"), bool)
        ):
            s.sort = (stored["column"], stored["descending"])
        raw = data.get("files")  # absent in a 0.1.0 file: the folder fields there are ignored
        if isinstance(raw, list):
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
                    continue
                cap = item.get("max_size")
                bake = item.get("bake")
                made_by = item.get("made_by")
                size = _size_from_json(item.get("size"))
                fs = FileSetting(
                    item["path"],
                    cap if _valid_cap(cap) and size is None else None,  # exclusive: the size wins
                    bake if isinstance(bake, bool) else False,
                    made_by if made_by in core.CONVERTERS else None,
                    size,
                )
                if fs.key not in seen:  # duplicates keep the first
                    seen.add(fs.key)
                    s._files.append(fs)
        return s

    def save(self) -> None:
        """Atomic write; raises OSError if the disk refuses (callers may surface it)."""
        if self.path is None:
            self.path = default_path()
        with self._lock:
            data = {
                "version": VERSION,
                "imagetopaa_path": self.imagetopaa_path,
                "converter": self.converter,
                "sort": {"column": self.sort[0], "descending": self.sort[1]},
                "files": [
                    {
                        "path": f.path,
                        "max_size": f.max_size,
                        "bake": f.bake,
                        "made_by": f.made_by,
                        "size": _size_to_json(f.size),
                    }
                    for f in self._files
                ],
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --- the file list ----------------------------------------------------------------------------

    def files(self) -> list[FileSetting]:
        """Snapshot copy, in add order."""
        with self._lock:
            return list(self._files)

    def add_files(self, paths: Iterable[str | Path]) -> list[str]:
        """Append files not yet listed (by key); returns the keys actually added."""
        added: list[str] = []
        with self._lock:
            have = {f.key for f in self._files}
            for p in paths:
                fs = FileSetting(str(Path(p).absolute()))
                if fs.key not in have:
                    have.add(fs.key)
                    self._files.append(fs)
                    added.append(fs.key)
        if added:
            self.save()
        return added

    def remove_files(self, keys: Iterable[str]) -> None:
        drop = set(keys)
        with self._lock:
            self._files = [f for f in self._files if f.key not in drop]
        self.save()

    def set_bake(self, keys: Iterable[str], on: bool) -> None:
        self._update(keys, bake=bool(on))

    def set_max_size(self, keys: Iterable[str], value: int | None) -> None:
        if not _valid_cap(value):
            raise ValueError(f"unsupported max size: {value}")
        if value is None:
            self._update(keys, max_size=None)
        else:  # a max size and an output size exclude each other: the one set last wins
            self._update(keys, max_size=value, size=None)

    def set_size(self, keys: Iterable[str], size: core.OutputSize | None) -> None:
        """Set the output size of the given files (None = Source). A size clears the file's max size."""
        if size is None:
            self._update(keys, size=None)
        else:
            self._update(keys, size=size, max_size=None)

    def set_converter(self, value: str) -> None:
        if value not in core.CONVERTERS:
            raise ValueError(f"unknown converter: {value}")
        with self._lock:
            self.converter = value
        self.save()

    def set_sort(self, column: str, descending: bool) -> None:
        if column not in SORT_COLUMNS:
            raise ValueError(f"unknown sort column: {column}")
        with self._lock:
            self.sort = (column, bool(descending))
        self.save()

    def set_made_by(self, key: str, value: str | None) -> None:
        """Record which converter wrote this file's .paa; saves only when it changes."""
        with self._lock:
            if all(f.made_by == value for f in self._files if f.key == key):
                return
        self._update([key], made_by=value)

    def _update(self, keys: Iterable[str], **changes) -> None:
        want = set(keys)
        with self._lock:
            self._files = [
                replace(f, **changes) if f.key in want else f for f in self._files
            ]
        self.save()
