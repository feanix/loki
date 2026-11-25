"""CLI entry: `python -m loki` opens the window; `--scenario` runs a headless script against app.App
(no Tk) and prints the state digest after each `dump`; `--screenshot` captures the real window offscreen.

Scenario commands, one per line (# comments, blank lines ignored; relative paths resolve against the
scenario file): drop <path>..., remove <path>, maxsize <path> off|2048|1024|512|256,
outsize <path> WxH [stretch|contain|pad] [R,G,B,A|default] [align] | outsize <path> off (no #RRGGBB: # starts a comment), toggle <path>,
sort <column> [asc|desc], allon, alloff, convertall, converter loki|bohemia, tool <path>, touch <path>, move <from> <to>, ls <dir>, paainfo <png path>,
wait <seconds>, idle [timeout], restart, about, dump. Quote a path that has spaces. Waits use the real clock
and are bounded, so a stuck step fails the run instead of hanging it. `idle` (default 60 s) pumps until no
listed file is changed-but-unsettled, no conversion is queued or running and no event is unapplied, then
fails with what was still pending if the timeout passes; prefer it to `wait` for "the conversion happened".
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

from loki import core
from loki.about import about_text
from loki.app import MAX_REPLACED_HINT, SIZE_REPLACED_HINT, App, parse_max_size

LOAD_TIMEOUT = 15.0
CONVERT_TIMEOUT = 120.0
MAX_WAIT = 30.0
IDLE_TIMEOUT = 60.0  # `idle` default: generous, a loaded machine is slow but a stuck run must still fail
MAX_IDLE = 300.0


class ScenarioError(Exception):
    pass


def _pump_until(app: App, done, timeout: float, what: str, still=None) -> None:
    deadline = time.monotonic() + timeout
    while not done():
        app.pump()
        if time.monotonic() > deadline:
            more = f"; still pending: {still()}" if still else ""
            raise ScenarioError(f"timed out waiting for {what}{more}")
        time.sleep(0.02)
    app.pump()


def _settled(app: App) -> None:
    """Wait until the watcher has baselined every listed file, so a later touch is a real save."""
    _pump_until(app, app.watching, LOAD_TIMEOUT, "the watcher to see the listed files")


def run_scenario(scenario: Path, settings_path: str, out=print, err=sys.stderr) -> int:
    base = scenario.parent

    def path_arg(text: str) -> str:
        p = Path(text.strip().strip('"'))
        return str(p if p.is_absolute() else (base / p).resolve())

    def paths_arg(text: str) -> list[str]:
        return [path_arg(t) for t in shlex.split(text, posix=False)]

    def new_app() -> App:
        # Fast polling so a scenario does not idle; the production defaults are 0.5 s / 1 s.
        a = App(settings_path, interval=0.05, settle=0.3)
        _settled(a)
        return a

    app = new_app()
    try:
        for n, raw in enumerate(scenario.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            cmd, _, arg = line.partition(" ")
            arg = arg.strip()
            try:
                if cmd == "drop":
                    app.add_files(paths_arg(arg))
                    _settled(app)
                elif cmd == "remove":
                    app.remove([core.file_key(p) for p in paths_arg(arg)])
                elif cmd == "maxsize":
                    target, _, value = arg.rpartition(" ")
                    if not target:
                        raise ScenarioError("maxsize <path> off|2048|1024|512|256")
                    app.set_max_size([core.file_key(path_arg(target))], parse_max_size(value))
                elif cmd == "outsize":
                    tokens = shlex.split(arg, posix=False)
                    if len(tokens) < 2 or len(tokens) > 5:
                        raise ScenarioError("outsize <path> WxH [fit] [colour|default] [align]  |  outsize <path> off")
                    key = core.file_key(path_arg(tokens[0]))
                    if tokens[1].lower() == "off" and len(tokens) == 2:
                        app.set_output_size([key], None)
                    else:
                        fit, colour, align = (tokens[2:] + [None, None, None])[:3]
                        colour = None if colour in (None, "default", "-") else colour
                        app.set_output_size([key], core.output_size(tokens[1], fit or "contain", colour, align or "centre"))
                    if app.message and app.message not in (SIZE_REPLACED_HINT, MAX_REPLACED_HINT):
                        raise ScenarioError(app.message)
                elif cmd == "toggle":
                    app.toggle([core.file_key(p) for p in paths_arg(arg)])
                elif cmd == "sort":
                    # `sort <column> [asc|desc]`: no direction = a header click (same column reverses)
                    name, _, way = arg.rpartition(" ")
                    if way.lower() in ("asc", "desc") and name:
                        app.sort_by(name, way.lower() == "desc")
                    elif arg:
                        app.sort_by(arg)
                    else:
                        raise ScenarioError("sort <column> [asc|desc]")
                    if app.message:  # an unknown column lands in the status message
                        raise ScenarioError(app.message)
                elif cmd == "allon":
                    app.set_all(True)
                elif cmd == "alloff":
                    app.set_all(False)
                elif cmd == "convertall":
                    app.convert_all()
                    _pump_until(app, lambda a=app: not a.converting, CONVERT_TIMEOUT, "convert all")
                elif cmd == "converter":
                    if arg not in core.CONVERTERS:
                        raise ScenarioError("converter loki|bohemia")
                    app.set_converter(arg)
                elif cmd == "tool":
                    app.set_tool(path_arg(arg))
                elif cmd == "touch":
                    target = Path(path_arg(arg))
                    _settled(app)
                    target.write_bytes(target.read_bytes())  # new mtime, same content: a "save"
                    os.utime(target)
                elif cmd == "move":
                    src, dst = paths_arg(arg)
                    os.replace(src, dst)
                elif cmd == "ls":
                    names = sorted(p.name for p in Path(path_arg(arg)).iterdir() if p.is_file())
                    out(f"ls {Path(arg).name}: " + ", ".join(names))
                    out("")
                elif cmd == "paainfo":
                    size = core.paa_size(Path(path_arg(arg)).with_suffix(".paa"))
                    out(f"paa {arg}: " + (f"{size[0]}x{size[1]}" if size else "unreadable"))
                    out("")
                elif cmd == "wait":
                    seconds = min(float(arg), MAX_WAIT)
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        app.pump()
                        time.sleep(0.02)
                    app.pump()
                elif cmd == "idle":
                    _pump_until(
                        app,
                        app.quiet,
                        min(float(arg), MAX_IDLE) if arg else IDLE_TIMEOUT,
                        "idle",
                        lambda a=app: ", ".join(a.pending()),
                    )
                elif cmd == "restart":
                    app.close()
                    app = new_app()
                elif cmd == "about":
                    out(about_text())
                    out("")
                elif cmd == "dump":
                    out(app.digest())
                    out(f"status: {app.summary()}")
                    out("")
                else:
                    raise ScenarioError(f"unknown command: {cmd}")
            except (ScenarioError, ValueError, OSError) as exc:
                print(f"error: line {n}: {exc}", file=err)
                return 2
        return 0
    finally:
        app.close()


def _run(argv: list[str] | None = None) -> int:
    _ensure_streams()
    ap = argparse.ArgumentParser(prog="loki", description="Convert PNG textures to PAA.")
    ap.add_argument("--settings", help="settings JSON path (default: %%APPDATA%%/Loki/settings.json)")
    ap.add_argument("--scenario", help="run a headless scenario file and print state digests")
    ap.add_argument("--screenshot", metavar="OUT.png", help="capture the window offscreen and exit")
    ap.add_argument(
        "--drop",
        metavar="PATH",
        nargs="+",
        help="with --screenshot: post these files to the window as a real drop, print the digest",
    )
    ap.add_argument(
        "--out",
        metavar="FILE",
        help="write scenario or screenshot output and errors to FILE (the windowed exe has no console to print to)",
    )
    args = ap.parse_args(argv)
    if args.scenario:
        if not args.settings:
            ap.error("--scenario needs --settings (it must never touch the real settings file)")
        scenario = Path(args.scenario).resolve()
        if not args.out:
            return run_scenario(scenario, args.settings)
        with open(args.out, "w", encoding="utf-8") as fh:
            return run_scenario(scenario, args.settings, out=lambda text: print(text, file=fh), err=fh)
    from loki import gui  # Tk only on the window paths

    if args.screenshot:
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                gui.screenshot(args.settings, Path(args.screenshot), drop=args.drop, echo=lambda t: print(t, file=fh))
        else:
            gui.screenshot(args.settings, Path(args.screenshot), drop=args.drop)
        return 0
    gui.run_window(args.settings)
    return 0


def crash_log_path() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / "Loki" / "crash.log"


CLI_POINTER = (
    "Loki.exe is the windowed program and has no command line. For the command line use Loki-cli.exe "
    "(same folder): Loki-cli.exe <inputs...>"
)


def _point_to_cli() -> int:
    """`Loki.exe convert ...` or `Loki.exe <input>`: no window; say where the CLI lives on stderr and exit 2.

    The windowed exe has no console of its own, so when stderr is not already redirected it attaches to the
    parent's console (cmd, PowerShell) and writes there."""
    err = sys.stderr
    if err is None and sys.platform == "win32":
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            try:
                err = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            except OSError:
                err = None
    if err is not None:
        print(CLI_POINTER, file=err, flush=True)
    return 2


def main(argv: list[str] | None = None) -> int:
    """Run Loki; an unexpected crash is written to crash.log (and shown in a dialog on the GUI path)."""
    first = (sys.argv[1:] if argv is None else argv)[:1]
    # `convert` or a bare input (a PNG, folder or pattern) is a CLI call: no window, point to Loki-cli.exe.
    # Options (--screenshot, --settings, scenario flags) are not.
    if first and not first[0].startswith("-"):
        return _point_to_cli()
    for stream in (sys.stdout, sys.stderr):  # the digest carries glyphs a cp1252 console cannot print
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        return _run(argv)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - last-resort handler: a windowed exe has no console
        import traceback

        from loki import __version__

        tb = traceback.format_exc()
        log = crash_log_path()
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(f"Loki {__version__}" + chr(10) + tb, encoding="utf-8")
        except OSError:
            pass
        args = sys.argv[1:] if argv is None else argv
        if not any(a.split("=")[0] in ("--scenario", "--screenshot") for a in args):
            try:
                from tkinter import Tk, messagebox

                root = Tk()
                root.withdraw()
                messagebox.showerror("Loki could not start", f"Loki could not start. Details: {log}")
                root.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
        return 1


def _ensure_streams() -> None:
    """A windowed (no console) build has sys.stdout/stderr = None; keep print() and argparse safe."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115 - lives for the whole process
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115


if __name__ == "__main__":
    sys.exit(main())
