"""Command line (decisions 19, 21, #018): `Loki-cli.exe <inputs...>` converts (a leading `convert` is still accepted, #62) PNGs to PAA and exits.

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
import textwrap
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
    "_cat, _can and _non (they use Bohemia's own noise generator)"
)

CONVERTED, SKIPPED, FLAGGED, NEEDS_TOOL, ERROR = "converted", "skipped", "flagged", "needs-tool", "error"


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would exit(2) itself; main() turns this into the exit code
        raise UsageError(message)


# Help text (#59, #62). Shown raw (RawDescriptionHelpFormatter), so line breaks here are the ones printed; keep
# the screen at 60 lines or fewer at 80 columns (tests/test_cli_help.py). Example lines start with "Loki-cli.exe" and
# that test runs each through the parser, so add new examples as more lines of the same shape.
GUIDE_LINE = "Full guide: https://github.com/feanix/loki#command-line"  # README heading "## Command line"
EXIT_CODE_LINES = (
    "0  all files converted or up to date",
    "1  any file flagged, needing Arma 3 Tools, or failed",
    "2  usage error (bad option, no PNG matched)",
)
USAGE = "Loki-cli.exe [options] input [input ...]"
NO_ARGS_LINE = "Run 'Loki-cli.exe --help' for all options."
EPILOG = (
    "Exit codes:\n"
    + "".join(f"  {line}\n" for line in EXIT_CODE_LINES)
    + "\n"
    "Examples:\n"
    "  In PowerShell, run it from its folder as .\\Loki-cli.exe (or add the\n"
    "  folder to PATH).\n"
    "  One file:\n"
    "    Loki-cli.exe textures\\wall_co.png\n"
    "  A folder with its subfolders, skipping PAAs that are up to date:\n"
    "    Loki-cli.exe textures --recursive --skip-up-to-date\n"
    "  A pattern. Keep the quotes so Loki expands the pattern itself:\n"
    '    Loki-cli.exe "textures\\*_co.png" --max-size 2048\n'
    "  A set output size (pad keeps the pixels, fills the rest):\n"
    "    Loki-cli.exe textures\\door_co.png --size 512x256 --fit pad\n"
    "  In a script, print only problems, then check the exit code:\n"
    "    Loki-cli.exe textures --quiet\n"
    "    if errorlevel 1 echo Some textures need attention            (cmd)\n"
    '    if ($LASTEXITCODE -ne 0) { "Some textures need attention" }  (PowerShell)\n'
    "\n"
    "Default --pad-colour by type: normal maps 128,128,255,255; _as, _ads, _adshq\n"
    "255,255,255,255; _ca, _lca, _cat, _can, _8888 and other alpha types 0,0,0,0;\n"
    "everything else 0,0,0,255.\n"
    "\n"
    "The word 'convert' before the inputs is optional (0.3.0 scripts still work).\n"
    "\n" + GUIDE_LINE
)


def _wrap(text: str) -> str:
    """Wrap a paragraph at 78 columns (raw help formatters do not wrap it themselves)."""
    return textwrap.fill(text, 78)


def _build_parser() -> argparse.ArgumentParser:
    """One parser: converting is the only action, so the options live at the top level (#62)."""
    ap = _Parser(
        prog="Loki-cli.exe",
        usage=USAGE,
        description=_wrap(
            "Loki-cli.exe converts PNG textures to Arma 3 PAA files and exits. Each PAA is written beside "
            "its PNG. Inputs: PNG files, glob patterns (expanded by Loki), folders (their *.png; add "
            "--recursive for subfolders). The built-in converter does most files. These go to Bohemia's "
            "ImageToPAA automatically: " + HANDOVER_CASES + "."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"Loki {__version__}")
    ap.add_argument("inputs", nargs="+", metavar="input", help="PNG file, glob pattern or folder")
    ap.add_argument("--max-size", metavar="SIZE", help="longest side: off, 2048, 1024, 512, 256 (default off)")
    ap.add_argument("--size", metavar="WxH", help="output size, powers of two 1-4096 (default: PNG size)")
    ap.add_argument("--fit", metavar="MODE", help="stretch, contain (default) or pad (no scaling)")
    ap.add_argument("--pad-colour", metavar="COLOUR", help="#RRGGBB, #RRGGBBAA or R,G,B,A (default: below)")
    ap.add_argument("--pad-color", dest="pad_colour", help=argparse.SUPPRESS)  # alias; not listed
    ap.add_argument("--pad-align", metavar="ALIGN", help="centre (default), top, bottom-left etc. (see the guide)")
    ap.add_argument(
        "--imagetopaa", metavar="PATH",
        help="Path to ImageToPAA.exe. Only needed if Arma 3 Tools is installed somewhere Loki cannot find; "
        "normally Loki finds it through Steam. If a file needs ImageToPAA and it is not found, that file is "
        "reported as 'needs Arma 3 Tools' and the exit code is 1.",
    )  # fmt: skip
    ap.add_argument("--jobs", type=int, metavar="N", help="parallel conversions (default: by CPU count)")
    ap.add_argument("--skip-up-to-date", action="store_true", help="do not reconvert a PNG whose PAA is up to date")
    ap.add_argument("--recursive", action="store_true", help="include subfolders of folder inputs")
    ap.add_argument(
        "--wait-locked", type=float, default=DEFAULT_WAIT_LOCKED, metavar="SEC",
        help=f"wait this long for a PNG or PAA held open by another program (default {DEFAULT_WAIT_LOCKED:g})",
    )  # fmt: skip
    ap.add_argument("--quiet", action="store_true", help="print only problems and the summary")
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
    def __init__(self, args: argparse.Namespace, max_size: int | None, size: core.OutputSize | None, tool: Path | None, out):
        self.args, self.max_size, self.size, self.tool, self.out = args, max_size, size, tool, out
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
        res = core.convert(png, self.tool, max_size, core.LOKI, size=self.size)
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
        entry = core.check_png(found.path, self.max_size, self.size)
        if entry.status == core.MISSING:
            self.line(ERROR, f"{shown}: file not found")
        elif entry.status == core.FLAGGED:
            self.line(FLAGGED, f"{shown}: {entry.reason}")
        elif entry.status == core.UP_TO_DATE and self.args.skip_up_to_date:
            self.line(SKIPPED, f"{shown} -> {found.path.with_suffix('.paa').name} (up to date)")
        elif core.route(found.path, core.LOKI, self.tool is not None) == core.NEEDS_TOOL:
            self.line(NEEDS_TOOL, f"{shown}: {core.needs_tool_reason(found.path, core.LOKI)}")
        else:
            key = core.file_key(found.path)
            self.sizes[key] = entry.size
            self.paths[key] = found.path
            self.open.add(key)
            self.pool.submit(key, found.path, self.max_size, "cli")
        return

    def _output_dims(self, key: str) -> tuple[int, int]:
        """The size of the PAA written for `key`: the output size, else the PNG's size capped by max size."""
        if self.size is not None:
            return self.size.width, self.size.height
        return core.target_size(*self.sizes[key], self.max_size) if self.sizes[key] else (0, 0)

    def on_result(self, key: str, res: core.ConvertResult, now: float) -> bool:
        """Print a finished file; False when it goes back to waiting on a lock instead."""
        shown = _display(self.paths[key])
        if res.ok:
            w, h = self._output_dims(key)
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


def _output_size(args: argparse.Namespace) -> core.OutputSize | None:
    """The --size options as an OutputSize, or None without --size. Every misuse is a usage error."""
    if args.size is None:
        for opt, value in (("--fit", args.fit), ("--pad-colour", args.pad_colour), ("--pad-align", args.pad_align)):
            if value is not None:
                raise UsageError(f"{opt} needs --size (it says how the PNG fills the output size)")
        return None
    if args.max_size is not None:
        raise UsageError("--size and --max-size cannot be used together (--size sets the output size)")
    try:
        return core.output_size(args.size, args.fit or "contain", args.pad_colour, args.pad_align or "centre")
    except ValueError as exc:
        raise UsageError(f"--size/--fit/--pad-colour/--pad-align: {exc}") from None


def _expect_files(args: argparse.Namespace) -> tuple[int | None, core.OutputSize | None, Path | None]:
    size = _output_size(args)
    try:
        max_size = parse_max_size(args.max_size or "off")
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
    return max_size, size, tool


def _convert(args: argparse.Namespace, out) -> int:
    max_size, size, tool = _expect_files(args)
    files = expand_inputs(args.inputs, args.recursive)
    if not files:
        raise UsageError("no PNG files matched the inputs")
    t0 = time.monotonic()
    run = _Run(args, max_size, size, tool, out)
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
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["convert"]:  # the 0.3.0 form; only the bare first word, so ".\\convert" stays an input (#62)
        argv = argv[1:]
    if not argv:  # nothing to do: short usage and where the options are, on stderr, exit 2
        print(f"usage: {USAGE}\n{NO_ARGS_LINE}", file=sys.stderr, flush=True)
        return 2
    try:
        return _convert(ap.parse_args(argv), sys.stdout)
    except UsageError as exc:
        print(f"Loki-cli.exe: error: {exc}", file=sys.stderr, flush=True)
        return 2
    except SystemExit as exc:  # --help and --version
        return exc.code if isinstance(exc.code, int) else 0
