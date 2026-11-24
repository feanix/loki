"""GUI model/controller for Loki. Imports no tkinter: the window (gui.py) and the headless scenario
runner (__main__.py) both drive this class, so every behaviour is testable without a display.

The list is the files the artist dragged in (decision 11). Nothing here scans a folder.

Threading: the watcher thread and the conversion pool's workers never touch state. They post events to a
queue.Queue; the owner (the Tk loop via after(), or the scenario runner) calls `pump()` to apply them.
So all state is mutated on one thread and needs no locks. Events carry a generation number, bumped
when the app closes, so a late event from a stopped worker is dropped.

Everything with a rule behind it is delegated: status/routing/convert/file identity to core.py, the list, caps,
toggles, converter choice and their persistence to settings.py, change detection and bake-on-change to watcher.py.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from loki import core
from loki.pool import ConvertPool
from loki.settings import Settings
from loki.watcher import Watcher

MAX_CHOICES: tuple[int | None, ...] = (None, *core.MAX_SIZES)
BAKE_HINT = "Bake converts on the next save. Use Convert all for files already changed."
TOOL_MISSING_HINT = "ImageToPAA.exe not found. Install Arma 3 Tools from Steam, or browse to it."
CONVERTER_LABELS = {core.LOKI: "Loki converter", core.BOHEMIA: "Bohemia ImageToPAA"}  # dropdown text
MADE_BY_LABELS = {core.LOKI: "Loki", core.BOHEMIA: "Bohemia"}  # row status suffix
EMPTY_HINT = "Drag PNG files here"
# The window's controls and columns, named once so the view and the digest cannot drift apart.
CONTROLS = ("Converter", "Convert all", "All on", "All off", "Remove", "About")
DROPDOWN_ARROW = "▾"  # small down triangle
COLUMNS = ("Bake", "File", "Folder", "Size", "Max size", "Status")


FOLDER_LABEL_CHARS = 40  # Folder column text longer than this loses its start


def folder_label(folder: str, limit: int = FOLDER_LABEL_CHARS) -> str:
    """Folder text for the Folder column: the end of the path (nearest the file) is what tells
    folders apart, so a long path keeps its tail behind a leading '...'. The full path is in the status bar."""
    if len(folder) <= limit:
        return folder
    return "..." + folder[-(limit - 3):]


@dataclass(frozen=True)
class Row:
    key: str  # core.file_key: the row's identity
    path: str
    max_size: int | None
    bake: bool
    status: str  # core status, or "error" when the last convert of this file failed
    reason: str | None  # flag reason or error text
    size: tuple[int, int] | None
    detail: str | None = None  # full technical text for the status bar when the row is selected
    busy: bool = False  # queued or running in the conversion pool
    made_by: str | None = None  # core.LOKI / core.BOHEMIA: who wrote this file's .paa last

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def folder(self) -> str:
        return str(Path(self.path).parent)

    @property
    def folder_text(self) -> str:
        return folder_label(self.folder)

    @property
    def size_text(self) -> str:
        return f"{self.size[0]}x{self.size[1]}" if self.size else "?"

    @property
    def max_text(self) -> str:
        return max_size_label(self.max_size)

    @property
    def max_cell(self) -> str:
        """Max size cell text: the arrow says clicking opens a menu."""
        return f"{self.max_text} {DROPDOWN_ARROW}"

    @property
    def bake_cell(self) -> str:
        """Bake cell text: a checkbox glyph says clicking toggles."""
        return "☑ On" if self.bake else "☐ Off"

    @property
    def status_text(self) -> str:
        if self.busy:
            return "Converting..."
        if self.status == core.UP_TO_DATE:
            return f"Up to date ({MADE_BY_LABELS[self.made_by]})" if self.made_by in MADE_BY_LABELS else "Up to date"
        if self.status == core.NEEDS_CONVERTING:
            return "Needs converting"
        if self.status == core.NEEDS_TOOL:
            return self.reason or "needs Arma 3 Tools"
        if self.status == core.MISSING:
            return "Missing"
        if self.status == core.IN_USE:
            return f"Waiting: {self.reason or 'file in use'}"
        if self.status == core.FLAGGED:
            return f"Flagged: {self.reason}"
        return f"Error: {self.reason}"


def max_size_label(value: int | None) -> str:
    return "Off" if value is None else str(value)


def parse_max_size(text: str) -> int | None:
    t = text.strip().lower()
    if t == "off":
        return None
    value = int(t)
    if value not in core.MAX_SIZES:
        raise ValueError(f"max size must be Off or one of {', '.join(map(str, core.MAX_SIZES))}")
    return value


class App:
    def __init__(
        self,
        settings_path: str | Path | None = None,
        find_tool: Callable[[str | None], Path] = core.find_imagetopaa,
        interval: float = 0.5,
        settle: float = 1.0,
        workers: int | None = None,
    ):
        self.settings = Settings.load(settings_path)
        self._find_tool = find_tool
        self.events: queue.Queue = queue.Queue()
        self.rows: list[Row] = []
        self.entries: dict[str, core.PngEntry] = {}  # key -> latest status of each listed file
        self.converting = False
        self.progress: tuple[int, int] | None = None
        self.message = ""  # last error/info for the status bar
        self.hint = ""  # shown when nothing more urgent is
        self.selection: list[str] = []  # keys selected in the view, for the detail line
        self.tool: Path | None = None
        self.tool_error: str | None = None
        self._errors: dict[str, tuple[tuple[int, int] | None, str]] = {}  # key -> (file sig, text)
        self._needs_tool_keys: set[str] = set()  # keys whose _errors text is a per-file "needs Arma 3 Tools (...)" (#025)
        self._waiting: dict[str, tuple[int, int] | None] = {}  # key -> file sig of a save locked during convert
        self._wait_why: dict[str, str] = {}  # key -> what it waits on ("file in use", "PAA in use")
        self._gen = 0
        # The Convert all run in flight: keys still to report, and how it is going.
        self._all_pending: set[str] = set()
        self._all_total = self._all_ok = self._all_failed = self._all_waiting = self._all_blocked = 0
        gen = self._gen
        self._pool = ConvertPool(
            self._convert_fn(),
            core.convert_workers() if workers is None else workers,
            on_result=lambda key, tags, res: self.events.put(("result", gen, key, tags, res)),
        )
        self._refresh_tool()
        self._rebuild_rows()
        self._watcher = Watcher(
            self.settings,
            self._pool.submit,
            on_change=lambda entries, results: self.events.put(("entries", gen, entries, results)),
            interval=interval,
            settle=settle,
        )
        self._watcher.start()

    # --- derived state ------------------------------------------------------------------------

    @property
    def tool_found(self) -> bool:
        return self.tool is not None

    @property
    def converter(self) -> str:
        return self.settings.converter

    @property
    def tool_warning(self) -> bool:
        """Show the missing-ImageToPAA banner: only when something needs it (Bohemia selected, or a row
        Loki's encoder cannot do), never just because the tool is absent."""
        return not self.tool_found and (
            self.converter == core.BOHEMIA or any(r.status == core.NEEDS_TOOL for r in self.rows)
        )

    def summary(self) -> str:
        """One-line status bar text."""
        if self.progress and self.converting:
            return f"Converting {self.progress[0]}/{self.progress[1]}..."
        if self.message:
            return self.message
        if len(self.selection) == 1:
            row = next((r for r in self.rows if r.key == self.selection[0]), None)
            if row:  # the full path, then the technical text when there is one
                return f"{row.name}: {row.detail}" if row.detail else row.path
        if self.hint:
            return self.hint
        if not self.rows:
            return "Drag PNG files onto the window to begin."
        n = len(self.rows)
        need = sum(r.status == core.NEEDS_CONVERTING for r in self.rows)
        flagged = sum(r.status == core.FLAGGED for r in self.rows)
        missing = sum(r.status == core.MISSING for r in self.rows)
        bad = sum(r.status == "error" for r in self.rows)
        blocked = sum(r.status == core.NEEDS_TOOL for r in self.rows)
        on = sum(r.bake for r in self.rows)
        gone = f", {missing} missing" if missing else ""
        tool = f", {blocked} need Arma 3 Tools" if blocked else ""
        return f"{n} png: {need} need converting, {flagged} flagged{gone}, {bad} errors{tool}; bake on for {on}"

    # --- actions (all catch their own exceptions) ----------------------------------------------

    def add_files(self, paths: Iterable[str | Path]) -> list[str]:
        """The one handler for a drop (the OS drop and the scenario `drop` command both call it).

        Keeps existing files with a .png suffix; skips folders, other files, paths that do not exist
        and files already listed. Adds rows with their status at once; writes nothing but settings.
        Returns the keys added."""
        try:
            listed = {f.key for f in self.settings.files()}
            take: list[str] = []
            not_png = not_found = dupes = 0
            for raw in paths:
                p = Path(raw)
                if p.is_dir() or p.suffix.lower() != ".png":
                    not_png += 1
                elif not p.is_file():
                    not_found += 1
                elif core.file_key(p) in listed:
                    dupes += 1
                else:
                    listed.add(core.file_key(p))
                    take.append(str(p))
            added = self.settings.add_files(take)
            skipped = [
                f"{n} {why}" for n, why in ((not_png, "not PNG"), (not_found, "not found"), (dupes, "already listed")) if n
            ]
            self.message = f"Added {len(added)} file{'' if len(added) == 1 else 's'}" + (
                f"; skipped {', '.join(skipped)}" if skipped else ""
            )
            self.hint = ""
            self._rebuild_rows()
            return added
        except Exception as exc:  # noqa: BLE001 -- a drop must never crash the window
            self._fail(exc)
            return []

    def remove(self, keys: Iterable[str]) -> None:
        """Take files off the list. The png and paa on disk are never touched."""
        try:
            keys = set(keys)
            gone = [r.key for r in self.rows if r.key in keys]
            if gone:
                self.settings.remove_files(gone)
                for k in self._pool.cancel(gone):  # not started yet: never converted (#010 crit 1)
                    if k in self._all_pending:
                        self._all_pending.discard(k)
                        self._all_total -= 1
                for k in gone:
                    self._errors.pop(k, None)
                    self._waiting.pop(k, None)
                    self._wait_why.pop(k, None)
                self.selection = [k for k in self.selection if k not in gone]
                self.message = f"Removed {len(gone)} file{'' if len(gone) == 1 else 's'} from the list"
                self._rebuild_rows()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def set_max_size(self, keys: Iterable[str], value: int | None) -> None:
        """Set the cap of the given rows only; their status is re-evaluated against it (D10)."""
        try:
            if value not in MAX_CHOICES:
                raise ValueError(f"unsupported max size: {value}")
            keys = {r.key for r in self.rows} & set(keys)
            self.settings.set_max_size(keys, value)
            self.message = ""
            self._rebuild_rows(recheck=keys)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def set_converter(self, value: str) -> None:
        """Choose the converter (persisted). Statuses never change: they come from each .paa header."""
        try:
            self.settings.set_converter(value)
            self.message = ""
            self._rebuild_rows()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def set_tool(self, path: str | Path) -> None:
        """Remember a user-chosen ImageToPAA path, but only once it validates."""
        try:
            found = self._find_tool(str(path))
            self.settings.imagetopaa_path = str(path)
            self.settings.save()
            self.tool, self.tool_error = found, None
            self.message = ""
            self._rebuild_rows()  # rows that waited for the tool become convertible
        except core.ToolNotFound as exc:
            self.message = str(exc)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def toggle(self, keys: list[str]) -> None:
        """Flip the bake toggle of rows. A mixed selection becomes uniformly the opposite of the first."""
        try:
            by_key = {r.key: r for r in self.rows}
            keys = [k for k in keys if k in by_key]
            if keys:
                on = not by_key[keys[0]].bake
                self._set_bake(keys, on)
                self.message = ""
                self.hint = BAKE_HINT if on and any(
                    by_key[k].status == core.NEEDS_CONVERTING for k in keys
                ) else ""  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def set_all(self, on: bool) -> None:
        try:
            self._set_bake([r.key for r in self.rows], on)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def convert_all(self) -> bool:
        """Submit every needs-converting listed png to the pool; returns at once. The run is done
        when the last of them reports (pump applies the results one by one)."""
        if self.converting:
            return False
        if self.converter == core.BOHEMIA and not self.tool:
            self.message = self.tool_error or TOOL_MISSING_HINT
            return False
        if not self.rows:
            self.message = "Nothing to convert: " + EMPTY_HINT.lower() + "."
            return False
        need = [r for r in self.rows if self.entries[r.key].status == core.NEEDS_CONVERTING]
        todo = [r for r in need if r.status != core.NEEDS_TOOL]  # those wait for Arma 3 Tools: not submitted
        self.message = ""
        self._all_pending = {r.key for r in todo}
        self._all_total, self._all_ok, self._all_failed, self._all_waiting = len(todo), 0, 0, 0
        self._all_blocked = len(need) - len(todo)
        self.converting = True
        self.progress = (0, self._all_total)
        for r in todo:
            self._pool.submit(r.key, Path(r.path), r.max_size, "all")
        self._rebuild_rows()
        self._finish_run_if_done()
        return True

    def close(self) -> None:
        self._pool.shutdown(wait=False)  # queued jobs dropped; a running tool finishes on its own
        self._drop_watcher(self._watcher).join(2.0)  # bounded: exit must not hang on a slow pass
        self._gen += 1

    # --- event loop side ------------------------------------------------------------------------

    def pump(self) -> bool:
        """Apply queued worker events. Returns True when visible state changed."""
        changed = False
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                changed = self._finish_run_if_done() or changed
                # A re-save with the toggle off changes no status, so the watcher stays silent;
                # the few errored files are stat'ed here instead.
                if self._expire_errors():
                    self._rebuild_rows()
                    changed = True
                return changed
            kind, gen = ev[0], ev[1]
            if gen != self._gen:
                continue
            changed = True
            try:
                if kind == "entries":
                    self._apply_entries(ev[2], ev[3])
                elif kind == "result":
                    self._apply_result(ev[2], ev[3], ev[4])
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)

    def idle(self) -> bool:
        return not self.converting and self.events.empty() and self._pool.idle()

    def watching(self) -> bool:
        """True once the watcher has baselined every listed file (a save after this is a real save)."""
        return all(self._watcher.is_baselined(f.key) for f in self.settings.files())

    # --- internals ------------------------------------------------------------------------------

    def _fail(self, exc: BaseException) -> None:
        self.message = f"{type(exc).__name__}: {exc}"

    def _refresh_tool(self) -> None:
        try:
            self.tool, self.tool_error = self._find_tool(self.settings.imagetopaa_path), None
        except core.ToolNotFound as exc:
            self.tool, self.tool_error = None, str(exc)

    def _set_bake(self, keys: list[str], on: bool) -> None:
        self.settings.set_bake(keys, on)
        self._rebuild_rows()

    def _convert_fn(self) -> Callable[[Path, int | None], core.ConvertResult]:
        def convert(png: Path, max_size: int | None) -> core.ConvertResult:
            tool = self.tool  # looked up at call time: Browse... takes effect without a restart
            converter = self.settings.converter
            if converter == core.BOHEMIA and tool is None:
                return core.ConvertResult(png, png.with_suffix(".paa"), False, TOOL_MISSING_HINT)
            return core.convert(png, tool, max_size, converter)

        return convert

    @staticmethod
    def _drop_watcher(watcher: Watcher) -> threading.Thread:
        """Stop a watcher off-thread: stop() joins, which must not block the Tk thread. Its late
        events are discarded by the generation check."""
        t = threading.Thread(target=watcher.stop, name="loki-watcher-stop", daemon=True)
        t.start()
        return t

    def _note_result(self, res: core.ConvertResult) -> None:
        key = core.file_key(res.png)
        if res.retry:  # locked during convert: waiting, not an error (shown as in_use until the file changes)
            self._errors.pop(key, None)
            self._waiting[key] = core.file_sig(res.png)
            self._wait_why[key] = res.error if res.error == core.PAA_IN_USE else "file in use"
        elif res.ok:
            self._errors.pop(key, None)
            self._waiting.pop(key, None)
        else:
            self._waiting.pop(key, None)
            # Keyed to the file's signature: saving the png again clears the error.
            self._errors[key] = (core.file_sig(res.png), res.error or "conversion failed")
            (self._needs_tool_keys.add if res.needs_tool else self._needs_tool_keys.discard)(key)

    def _apply_result(self, key: str, tags: frozenset[str], res: core.ConvertResult) -> None:
        """One job reported (Convert all or bake): refresh that row only, count it toward the run."""
        self._note_result(res)
        if res.ok and res.converter:
            self.settings.set_made_by(key, res.converter)
        f = next((f for f in self.settings.files() if f.key == key), None)
        if f:
            self.entries[key] = core.check_png(f.path, f.max_size)
            if res.retry and "bake" in tags:  # the watcher submits it again once readable
                self._watcher.retry(key, core.file_sig(Path(f.path)), self._wait_why.get(key, "file in use"))
        if "all" in tags and key in self._all_pending:  # a bake result never ends the run's job
            self._all_pending.discard(key)
            self._all_ok += res.ok
            self._all_failed += not res.ok and not res.retry
            self._all_waiting += res.retry
            self.progress = (self._all_total - len(self._all_pending), self._all_total)
        self._expire_errors()
        self._rebuild_rows()

    def _finish_run_if_done(self) -> bool:
        """End the Convert all run once its last job reported or was cancelled."""
        if not self.converting or self._all_pending:
            return False
        self.converting = False
        self.progress = None
        self.message = (
            f"Converted {self._all_ok} file(s)"
            + (f", {self._all_failed} failed" if self._all_failed else "")
            + (f", {self._all_waiting} waiting: file in use" if self._all_waiting else "")
            + (f", {self._all_blocked} need Arma 3 Tools" if self._all_blocked else "")
        )
        return True

    def _apply_entries(self, entries: list[core.PngEntry], results: list[core.ConvertResult]) -> None:
        caps = {f.key: f.max_size for f in self.settings.files()}
        for e in entries:
            key = core.file_key(e.path)
            if e.status == core.NEEDS_CONVERTING and key in caps:
                # The watcher read this before a convert replaced the .paa (or while it was briefly
                # held): applied as is it would undo the fresher result, so look again now.
                e = core.check_png(e.path, caps[key])
            self.entries[key] = e
        for res in results:
            self._note_result(res)
        self._expire_errors()
        self._rebuild_rows()

    def _expire_errors(self) -> bool:
        """Drop row errors whose png changed or vanished since the failure."""
        stale = [
            key for key, (sig, _text) in self._errors.items()
            if key not in self.entries or core.file_sig(self.entries[key].path) != sig
        ]  # fmt: skip
        for key in stale:
            del self._errors[key]
        gone = [
            key for key, sig in self._waiting.items()
            if key not in self.entries or core.file_sig(self.entries[key].path) != sig
        ]  # fmt: skip
        for key in gone:
            del self._waiting[key]
            self._wait_why.pop(key, None)
        return bool(stale or gone)

    def _needs_tool(self, path: str, entry: core.PngEntry) -> bool:
        """True when the Loki converter is selected and this file can only be done by an absent ImageToPAA.
        (With Bohemia selected a missing tool is the banner's business, not a per-row state.)"""
        return (
            self.converter == core.LOKI
            and core.route(path, entry.size, core.LOKI, self.tool_found) == core.NEEDS_TOOL
        )

    def _rebuild_rows(self, recheck: Iterable[str] = ()) -> None:
        """Rows follow the settings list (order, caps, toggles). A file with no status yet, or one
        whose cap just changed (`recheck`), is checked here so a new row never waits for the watcher."""
        recheck = set(recheck)
        files = self.settings.files()
        listed = {f.key for f in files}
        for key in set(self.entries) - listed:
            del self.entries[key]
        busy = self._pool.busy_keys()
        rows = []
        for f in files:
            if f.key not in self.entries or f.key in recheck:
                self.entries[f.key] = core.check_png(f.path, f.max_size)
            e = self.entries[f.key]
            err = self._errors.get(f.key)
            if f.key in self._waiting and e.status == core.NEEDS_CONVERTING:
                e = replace(e, status=core.IN_USE, reason=self._wait_why.get(f.key, "file in use"))
            if err and e.status not in (core.FLAGGED, core.MISSING):
                status, reason = ("error", err[1]) if f.key not in self._needs_tool_keys else (core.NEEDS_TOOL, err[1])
            else:
                status, reason = e.status, e.reason
                if status == core.NEEDS_CONVERTING and self._needs_tool(f.path, e):
                    status, reason = core.NEEDS_TOOL, core.needs_tool_reason(f.path, e.size)
            detail = err[1] if status in ("error", core.NEEDS_TOOL) and err else e.detail
            rows.append(
                Row(f.key, f.path, f.max_size, f.bake, status, reason, e.size, detail, f.key in busy, f.made_by)
            )
        self.rows = rows

    # --- digest ---------------------------------------------------------------------------------

    def digest(self) -> str:
        """Plain-text dump of controls, settings and rows (list order)."""
        s = self.settings
        lines = [
            f"controls: {', '.join(CONTROLS)}",
            f"columns: {', '.join(COLUMNS)}",
            "settings:",
            f"  imagetopaa_path: {s.imagetopaa_path}",
            f"  tool_found: {'yes' if self.tool_found else 'no'}",
            f"  converter: {s.converter}",
            f"  converters: {', '.join(CONVERTER_LABELS.values())}",
            f"  files: {len(s.files())}",
            "rows:",
        ]
        for r in self.rows:
            status = r.status + (f": {r.reason}" if r.reason else "")
            made = f" | made_by={r.made_by}" if r.made_by else ""
            lines.append(
                f"  {r.path} | max={r.max_cell.lower()} | bake={r.bake_cell.lower()} | {status} | {r.size_text}{made}"
            )
        return "\n".join(lines)
