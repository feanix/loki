"""Smoke-check dist/Loki.exe from a folder outside the repo with a minimal PATH (manual, not pytest).

Usage: python build/check_exe.py WORKDIR [EXEDIR]   (WORKDIR must be outside the repo; EXEDIR holds Loki.exe and
Loki-cli.exe, default dist/, e.g. an unpacked release zip)
Runs the demo scenario (+ about) with --out, then --screenshot --drop: the exe posts a real WM_DROPFILES
to its own window and this script asserts the dropped files appear as rows in the digest it prints.
Also writes the screenshot-with-mixed-caps fixture (shot.scn) and takes a second screenshot of it.
Then converts a _co, a _sky and a _cat PNG with ImageToPAA hidden (settings point at a path that does not exist, and
the minimal PATH holds no Arma 3 Tools): the exe's built-in Loki converter must write the _co and _sky .paa
(made_by=loki; _sky moved into Loki in 0.3.1, #031) and the _cat row (a noise suffix, still ImageToPAA-only) must
say it needs Arma 3 Tools. No Rust or Python is on that PATH either.
Then checks dist/Loki-cli.exe (see check_cli): PE subsystems, --help/--version, one conversion per input kind,
exit codes 0/1/2, output through cmd.exe and powershell.exe piped and redirected to a file, and that
`Loki.exe convert` opens no window and prints the pointer with exit 2.
Windows only. Never opens a visible window (the screenshot window is offscreen and near-transparent).
"""

from __future__ import annotations

import codecs
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "scenarios"))
from build_demo import build

sys.path.insert(0, str(ROOT / "src"))
from loki.app import Row


def row_head(path: Path) -> str:
    """A freshly dropped file's digest row up to the status, built from the app's own Row cells."""
    r = Row(str(path), str(path), None, False, "", None, None)
    return f"  {r.path} | max={r.max_cell.lower()} | auto watch={r.bake_cell.lower()} | "


def dropped_rows_ok(digest: str, dropped: list[Path]) -> bool:
    """Every dropped file has its row (default cells) in the digest and the file count matches."""
    return all(row_head(p) in digest for p in dropped) and f"files: {len(dropped)}" in digest


PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"  # not on the minimal PATH


def decode_output(raw: bytes) -> str:
    """PowerShell 5.1 redirects as UTF-16 with a BOM, cmd keeps the program's UTF-8 bytes."""
    boms = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
    return raw.decode("utf-16" if raw[:2] in boms else "utf-8", errors="replace")


def log_text(path: Path) -> str:
    return decode_output(path.read_bytes()).strip()


def pe_subsystem(exe: Path) -> int:
    """PE optional-header Subsystem: 2 = GUI (no console window), 3 = console."""
    data = exe.read_bytes()[:4096]
    pe = int.from_bytes(data[0x3C:0x40], "little")
    return int.from_bytes(data[pe + 24 + 68 : pe + 24 + 70], "little")


def windows_of(pid: int) -> list[int]:
    """Top-level windows owned by a process (visible or not)."""
    import ctypes
    from ctypes import wintypes

    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        owner = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(proc(cb), 0)
    return found


def pointer_check(exe: Path, env: dict, work: Path) -> bool:
    """`Loki.exe convert x.png`: exit 2, the pointer on stderr, and no window at any moment while it runs."""
    proc = subprocess.Popen([str(exe), "convert", "x.png"], cwd=work, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # fmt: skip
    seen = windows_of(proc.pid)
    _out, err = proc.communicate(timeout=60)
    print("Loki.exe convert x.png ->", proc.returncode, repr(err.strip()), "windows seen:", len(seen))
    return proc.returncode == 2 and "Loki-cli.exe" in err and not seen


def check_cli(work: Path, env: dict, exe_dir: Path = ROOT / "dist") -> bool:
    gui, cli = exe_dir / "Loki.exe", exe_dir / "Loki-cli.exe"
    ok = pe_subsystem(gui) == 2 and pe_subsystem(cli) == 3
    print("PE subsystem Loki.exe (2 = GUI):", pe_subsystem(gui), "| Loki-cli.exe (3 = console):", pe_subsystem(cli))
    ok = pointer_check(gui, env, work) and ok
    d = work / "cli"
    shutil.rmtree(d, ignore_errors=True)
    for name in ("tex/a_co.png", "tex/b_co.png", "tex/sub/c_co.png", "tex/glob/g_co.png"):
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        Image.effect_noise((256, 256), 60).convert("RGB").save(d / name)
    Image.effect_noise((300, 200), 60).convert("RGB").save(d / "odd_co.png")

    def run(argv: list[str], shell: str | None = None, redirect: Path | None = None):
        """Run Loki-cli.exe directly, or through cmd.exe / powershell.exe (stdout piped, or redirected to a file)."""
        if shell == "cmd":
            q = " ".join(f'"{a}"' for a in [str(cli), *argv])
            cmd = f'cmd.exe /c "{q}' + (f' > "{redirect}"' if redirect else "") + '"'
        elif shell == "powershell":
            q = " ".join(f"'{a}'" for a in [str(cli), *argv])
            cmd = f'{PS} -NoProfile -Command "& {q}' + (f" > '{redirect}'" if redirect else "") + '"'
        else:
            cmd = [str(cli), *argv]
        shell_env = {**env, "PATHEXT": ".COM;.EXE;.BAT;.CMD"}  # PowerShell prints nothing for an exe without PATHEXT
        r = subprocess.run(cmd, cwd=d, env=shell_env, capture_output=True, text=True, timeout=180, check=False)
        text = r.stdout
        if redirect:  # PowerShell 5.1's > writes UTF-16 with a BOM, cmd's writes the program's bytes
            raw = redirect.read_bytes()
            text = decode_output(raw)
        return r.returncode, text, r.stderr

    checks: list[tuple[str, bool]] = []

    def expect(label: str, got, code: int, needle: str) -> None:
        good = got[0] == code and needle in (got[1] + got[2])
        print(f"{label}: exit {got[0]} {'ok' if good else 'FAIL'} | {got[1].strip().splitlines()[-1] if got[1].strip() else got[2].strip()[:80]}")
        checks.append((label, good))

    expect("--help", run(["--help"]), 0, "Loki-cli.exe [options] input")
    expect("--version", run(["--version"]), 0, "Loki ")
    expect("no arguments", run([]), 2, "Run 'Loki-cli.exe --help' for all options.")
    expect("file", run(["tex/a_co.png"]), 0, "converted  tex\\a_co.png -> a_co.paa (256x256, loki,")
    expect("file, convert form", run(["convert", "tex/a_co.png"]), 0, "converted  tex\\a_co.png -> a_co.paa (256x256, loki,")
    expect("glob", run(["tex/glob/*.png"]), 0, "g_co.paa")
    expect("folder", run(["tex"]), 0, "b_co.paa")
    expect("folder --recursive", run(["tex", "--recursive", "--skip-up-to-date"]), 0, "c_co.paa")
    expect("exit 1 flagged", run(["odd_co.png"]), 1, "flagged    odd_co.png: 300x200: sides must be powers of two")
    expect("exit 2 no match", run(["nothing*.png"]), 2, "no PNG")
    expect("exit 2 bad option", run(["--nope", "tex"]), 2, "unrecognized")
    for shell in ("cmd", "powershell"):
        expect(f"{shell} piped", run(["tex/a_co.png"], shell), 0, "done: 1 converted")
        log = work / f"cli_{shell}.log"
        expect(f"{shell} redirected", run(["tex/a_co.png"], shell, log), 0, "done: 1 converted")
        print(f"  log {log}:", log_text(log).replace(chr(10), " / "))
    return ok and all(good for _l, good in checks)


def main() -> int:
    work = Path(sys.argv[1]).resolve()
    if ROOT in (work, *work.parents):
        print("WORKDIR must be outside the repo")
        return 2
    work.mkdir(parents=True, exist_ok=True)
    data = build(work)
    for stale in ("settings.json", "drop_settings.json", "shot_settings.json"):
        (work / stale).unlink(missing_ok=True)  # a rerun must start from an empty list
    scn = work / "demo.scn"
    scn.write_text(scn.read_text(encoding="utf-8") + "about\n", encoding="utf-8")
    exe_dir = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT / "dist"
    exe = exe_dir / "Loki.exe"
    env = {"PATH": r"C:\Windows\System32;C:\Windows", "SystemRoot": r"C:\Windows",
           "APPDATA": str(work / "appdata"), "TEMP": str(work / "tmp")}  # fmt: skip
    (work / "tmp").mkdir(exist_ok=True)
    print("PATH =", env["PATH"], "| cwd =", work)

    log = work / "scenario.log"
    r1 = subprocess.run([str(exe), "--scenario", str(scn), "--settings", str(work / "settings.json"),
                         "--out", str(log)], cwd=work, env=env, timeout=180, check=False)  # fmt: skip
    print("scenario exit", r1.returncode, "->", log)
    text = log.read_text(encoding="utf-8") if log.exists() else ""

    # A real OS-level drop message into the packaged exe's own window, then the digest it prints.
    dropped = [data / "textures" / "stone_co.png", data / "weapons" / "rifle_co.png", data / "textures" / "grass_co.png"]
    shot, dlog = work / "drop_shot.png", work / "drop.log"
    r2 = subprocess.run([str(exe), "--settings", str(work / "drop_settings.json"), "--screenshot", str(shot),
                         "--drop", *map(str, dropped), "--out", str(dlog)],
                        cwd=work, env=env, timeout=120, check=False)  # fmt: skip
    digest = dlog.read_text(encoding="utf-8") if dlog.exists() else ""
    print("drop screenshot exit", r2.returncode, "->", shot, shot.exists() and shot.stat().st_size)
    # Status varies (the demo scenario above already converted some of these); the rows must be there.
    rows_ok = dropped_rows_ok(digest, dropped)
    print("dropped rows in the exe's digest:", rows_ok)

    # Mixed caps and statuses for the requester's screenshot: build the list by scenario, then capture.
    shot_scn = work / "shot.scn"
    r3 = subprocess.run([str(exe), "--scenario", str(shot_scn), "--settings", str(work / "shot_settings.json"),
                         "--out", str(work / "shot.log")], cwd=work, env=env, timeout=180, check=False)  # fmt: skip
    mixed = work / "shot.png"
    r4 = subprocess.run([str(exe), "--settings", str(work / "shot_settings.json"), "--screenshot", str(mixed)],
                        cwd=work, env=env, timeout=120, check=False)  # fmt: skip
    print("mixed-caps screenshot exit", r3.returncode, r4.returncode, "->", mixed, mixed.exists() and mixed.stat().st_size)

    # Built-in converter with ImageToPAA hidden: the settings path is forced to a file that does not exist.
    loki_dir = work / "loki_conv"
    loki_dir.mkdir(exist_ok=True)
    for name in ("brick_co.paa", "brick_co.png", "wall_sky.paa", "wall_sky.png", "wall_cat.png"):
        (loki_dir / name).unlink(missing_ok=True)
    Image.effect_noise((256, 256), 60).convert("RGB").save(loki_dir / "brick_co.png")
    Image.effect_noise((256, 256), 60).convert("RGB").save(loki_dir / "wall_sky.png")
    Image.effect_noise((256, 256), 60).convert("RGB").save(loki_dir / "wall_cat.png")
    hidden = work / "hidden_settings.json"
    hidden.write_text(json.dumps({"version": 2, "imagetopaa_path": str(work / "no-tools" / "ImageToPAA.exe"),
                                  "files": []}), encoding="utf-8")  # fmt: skip
    (work / "loki.scn").write_text(f"drop {loki_dir / 'brick_co.png'} {loki_dir / 'wall_sky.png'} {loki_dir / 'wall_cat.png'}\n"
                                   "convertall\ndump\n", encoding="utf-8")  # fmt: skip
    r5 = subprocess.run([str(exe), "--scenario", str(work / "loki.scn"), "--settings", str(hidden),
                         "--out", str(work / "loki.log")], cwd=work, env=env, timeout=180, check=False)  # fmt: skip
    ltext = (work / "loki.log").read_text(encoding="utf-8") if (work / "loki.log").exists() else ""
    loki_ok = (r5.returncode == 0 and (loki_dir / "brick_co.paa").is_file() and (loki_dir / "wall_sky.paa").is_file()
               and not (loki_dir / "wall_cat.paa").exists() and "tool_found: no" in ltext
               and ltext.count("up_to_date | 256x256 | made_by=loki") == 2
               and "needs_tool: needs Arma 3 Tools for _cat" in ltext)  # fmt: skip
    print("Loki converter with ImageToPAA hidden (_co and _sky converted by Loki, _cat needs the tool):", loki_ok)

    cli_ok = check_cli(work, env, exe_dir)
    print("Loki-cli.exe and Loki.exe convert pointer:", cli_ok)

    ok = (cli_ok and loki_ok and r1.returncode == 0 and "MIT + Commons Clause" in text and "Astrid Winters" in text and "status:" in text and r2.returncode == 0
          and shot.exists() and rows_ok and r3.returncode == 0 and r4.returncode == 0 and mixed.exists())  # fmt: skip
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
