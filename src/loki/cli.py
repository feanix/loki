"""Command line (decisions 19, 21, #018): `Loki-cli.exe convert <inputs...>` converts PNGs to PAA and exits.

Reuses the GUI's pieces (core.check_png / route / convert, pool.ConvertPool, the watcher's backoff
schedule) and never reads or writes the GUI's settings or file list. No Tk. One line per file on stdout,
flushed as each file finishes, then a summary. Exit 0: all converted or skipped; 1: any flagged,
needs-tool or error; 2: usage error (bad option or value, no PNG matched). A result with retry=True
(png or output .paa held open, #015/#028) is resubmitted with the #015 backoff until --wait-locked
seconds have passed since its first wait.
"""

from __future__ import annotations

import argparse
import glob
import os
import queue
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from loki import __version__, core
from loki.app import parse_max_size
from loki.pool import ConvertPool
from loki.watcher import backoff_due

DEFAULT_WAIT_LOCKED = 30.0
RETRY_PASS = 0.5  # seconds between backoff passes (the watcher's default poll interval)
KIND_WIDTH = 11
# Plain-words list of what the built-in converter hands over (README "Known limits"); the suffix names in it
# are checked against the encoder by tests/test_cli.py, so it cannot drift.
HANDOVER_CASES = (
    "some textures with transparency (soft alpha, or cut-out alpha such as hair cards), _sky, _cat, _can, _non, "
    "unknown suffixes, and PNGs over 4096 on a side"
)

CONVERTED, SKIPPED, FLAGGED, NEEDS_TOOL, ERROR = "converted", "skipped", "flagged", "needs-tool", "error"


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would exit(2) itself; main() turns this into the exit code
        raise UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="Loki-cli.exe", description="Convert PNG textures to Arma 3 PAA and exit.")
    ap.add_argument("--version", action="version", version=f"Loki {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="convert")
    cv = sub.add_parser(
        "convert",
        prog="Loki-cli.exe convert",
        help="convert PNG files, globs or folders",
        description="Convert PNGs to PAA beside each PNG. Inputs: PNG files, glob patterns (expanded by Loki), "
        "folders (their *.png; add --recursive for subfolders). "
        "The built-in converter does most files. These go to Bohemia's ImageToPAA automatically: "
        + HANDOVER_CASES + ".",
    )
    cv.add_argument("inputs", nargs="+", metavar="input", help="PNG file, glob pattern or folder")
    cv.add_argument("--max-size", default="off", metavar="off|2048|1024|512|256", help="cap the longest side (default off)")
    cv.add_argument(
        "--imagetopaa", metavar="PATH",
        help="Path to ImageToPAA.exe. Only needed if Arma 3 Tools is installed somewhere Loki cannot find; "
        "normally Loki finds it through Steam. If a file needs ImageToPAA and it is not found, that file is "
        "reported as 'needs Arma 3 Tools' and the exit code is 1.",
    )  # fmt: skip
    cv.add_argument("--jobs", type=int, metavar="N", help="parallel conversions (default: by CPU count)")
    cv.add_argument("--skip-up-to-date", action="store_true", help="do not reconvert a PNG whose PAA is up to date")
    cv.add_argument("--recursive", action="store_true", help="include subfolders of folder inputs")
    cv.add_argument(
        "--wait-locked", type=float, default=DEFAULT_WAIT_LOCKED, metavar="SECONDS",
        help=f"wait this long for a PNG or PAA held open by another program (default {DEFAULT_WAIT_LOCKED:g})",
    )  # fmt: skip
    cv.add_argument("--quiet", action="store_true", help="print only problems and the summary")
    return ap


@dataclass
class _Found:
    path: Path
    problem: str | None = None  # set for an explicit input that cannot be converted at all


def _folder_pngs(folder: Path, recursive: bool) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(p for p in it if p.suffix.lower() == ".png" and p.is_file())


def expand_inputs(inputs: list[str], recursive: bool) -> list[_Found]:
    """Files, globs and folders -> PNG candidates, in input order, each file once (core.file_key)."""
    out: list[_Found] = []
    seen: set[str] = set()

    def add(path: Path, problem: str | None = None) -> None:
        key = core.file_key(path)
        if key not in seen:
            seen.add(key)
            out.append(_Found(path, problem))

    for text in inputs:
        p = Path(text)
        if p.is_dir():
            for f in _folder_pngs(p, recursive):
                add(f)
        elif p.is_file():
            add(p, None if p.suffix.lower() == ".png" else "not a PNG file")
        elif glob.has_magic(text):  # the shells on Windows do not expand globs
            for m in sorted(glob.glob(text, recursive=True)):
                mp = Path(m)
                if mp.is_file() and mp.suffix.lower() == ".png":
                    add(mp)
                elif mp.is_dir():
                    for f in _folder_pngs(mp, recursive):
                        add(f)
        else:
            add(p, "file not found")
    return out


def _display(path: Path) -> str:
    """The path as the user would type it: relative to the current folder when under it."""
    try:
        rel = os.path.relpath(path)
    except ValueError:  # another drive
        return str(path)
    return str(path) if rel.startswith("..") else rel


@dataclass
class _Wait:
    since: float
    last: float
    running: bool = False


class _Run:
    def __init__(self, args: argparse.Namespace, max_size: int | None, tool: Path | None, out):
        self.args, self.max_size, self.tool, self.out = args, max_size, tool, out
        self.counts = {CONVERTED: 0, SKIPPED: 0, FLAGGED: 0, NEEDS_TOOL: 0, ERROR: 0}
        self.results: queue.Queue = queue.Queue()
        self.sizes: dict[str, tuple[int, int] | None] = {}
        self.paths: dict[str, Path] = {}
        self.took: dict[str, float] = {}
        self.waiting: dict[str, _Wait] = {}
        self.open: set[str] = set()  # keys handed to the pool and not yet printed
        self.last_error: dict[str, str | None] = {}
        self.pool = ConvertPool(self._convert, workers=args.jobs, on_result=lambda k, _t, r: self.results.put((k, r)))

    def _convert(self, png: Path, max_size: int | None) -> core.ConvertResult:
        t0 = time.monotonic()
        res = core.convert(png, self.tool, max_size, core.LOKI)
        self.took[core.file_key(png)] = time.monotonic() - t0
        return res

    def line(self, kind: str, text: str) -> None:
        self.counts[kind] += 1
        if self.args.quiet and kind in (CONVERTED, SKIPPED):
            return
        print(f"{kind:<{KIND_WIDTH}}{text}", file=self.out, flush=True)

    def triage(self, found: _Found) -> None:
        """Report what needs no conversion at once; anything else goes to the pool."""
        shown = _display(found.path)
        if found.problem:
            self.line(ERROR, f"{shown}: {found.problem}")
            return
        entry = core.check_png(found.path, self.max_size)
        if entry.status == core.MISSING:
            self.line(ERROR, f"{shown}: file not found")
        elif entry.status == core.FLAGGED:
            self.line(FLAGGED, f"{shown}: {entry.reason}")
        elif entry.status == core.UP_TO_DATE and self.args.skip_up_to_date:
            self.line(SKIPPED, f"{shown} -> {found.path.with_suffix('.paa').name} (up to date)")
        elif core.route(found.path, entry.size, core.LOKI, self.tool is not None) == core.NEEDS_TOOL:
            self.line(NEEDS_TOOL, f"{shown}: {core.needs_tool_reason(found.path, entry.size, core.LOKI)}")
        else:
            key = core.file_key(found.path)
            self.sizes[key] = entry.size
            self.paths[key] = found.path
            self.open.add(key)
            self.pool.submit(key, found.path, self.max_size, "cli")
        return

    def on_result(self, key: str, res: core.ConvertResult, now: float) -> bool:
        """Print a finished file; False when it goes back to waiting on a lock instead."""
        shown = _display(self.paths[key])
        if res.ok:
            w, h = core.target_size(*self.sizes[key], self.max_size) if self.sizes[key] else (0, 0)
            self.line(CONVERTED, f"{shown} -> {res.paa.name} ({w}x{h}, {res.converter}, {self.took.get(key, 0.0):.2f} s)")
            return True
        if res.retry:
            wait = self.waiting.setdefault(key, _Wait(now, now))
            wait.running = False
            if now - wait.since < self.args.wait_locked:
                return False
        self.waiting.pop(key, None)
        self.line(NEEDS_TOOL if res.needs_tool else ERROR, f"{shown}: {res.error}")
        return True

    def backoff_pass(self, now: float) -> None:
        for key, wait in list(self.waiting.items()):
            if wait.running:
                continue
            if now - wait.since >= self.args.wait_locked:
                self.waiting.pop(key)
                self.open.discard(key)
                self.line(ERROR, f"{_display(self.paths[key])}: {self.last_error[key]}")
            elif backoff_due(wait.since, wait.last, now):
                wait.last, wait.running = now, True
                self.pool.submit(key, self.paths[key], self.max_size, "cli")

    def run(self, files: list[_Found]) -> None:
        for f in files:
            self.triage(f)
        next_pass = time.monotonic() + RETRY_PASS
        while self.open:
            try:
                key, res = self.results.get(timeout=0.05)
            except queue.Empty:
                pass
            else:
                self.last_error[key] = res.error
                if self.on_result(key, res, time.monotonic()):
                    self.open.discard(key)
            now = time.monotonic()
            if now >= next_pass:
                next_pass = now + RETRY_PASS
                self.backoff_pass(now)
        self.pool.shutdown()


def _expect_files(args: argparse.Namespace) -> tuple[int | None, Path | None]:
    try:
        max_size = parse_max_size(args.max_size)
    except ValueError as exc:
        raise UsageError(f"--max-size: {exc}") from None
    if args.jobs is not None and args.jobs < 1:
        raise UsageError("--jobs must be 1 or more")
    if args.wait_locked < 0:
        raise UsageError("--wait-locked must be 0 or more")
    tool: Path | None
    if args.imagetopaa:
        try:
            tool = core.find_imagetopaa(args.imagetopaa)
        except core.ToolNotFound as exc:
            raise UsageError(str(exc)) from None
    else:
        try:
            tool = core.find_imagetopaa()
        except core.ToolNotFound:
            tool = None  # only a file that needs it reports "needs Arma 3 Tools"
    return max_size, tool


def _convert(args: argparse.Namespace, out) -> int:
    max_size, tool = _expect_files(args)
    files = expand_inputs(args.inputs, args.recursive)
    if not files:
        raise UsageError("no PNG files matched the inputs")
    t0 = time.monotonic()
    run = _Run(args, max_size, tool, out)
    run.run(files)
    c = run.counts
    print(
        f"done: {c[CONVERTED]} converted, {c[SKIPPED]} skipped, {c[FLAGGED]} flagged, "
        f"{c[NEEDS_TOOL]} need Arma 3 Tools, {c[ERROR]} errors ({time.monotonic() - t0:.1f} s)",
        file=out, flush=True,
    )  # fmt: skip
    return 0 if c[FLAGGED] + c[NEEDS_TOOL] + c[ERROR] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a file name must never crash a print; keep the console's code page
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = _build_parser()
    try:
        args = ap.parse_args(sys.argv[1:] if argv is None else argv)
        if args.command != "convert":
            ap.print_help()
            raise UsageError("no command given: use 'convert'")
        return _convert(args, sys.stdout)
    except UsageError as exc:
        print(f"Loki-cli.exe: error: {exc}", file=sys.stderr, flush=True)
        return 2
    except SystemExit as exc:  # --help and --version
        return exc.code if isinstance(exc.code, int) else 0
