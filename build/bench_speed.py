"""Benchmark: Loki's built-in converter vs Bohemia ImageToPAA on one texture, median of several runs.

    python build/bench_speed.py [--size 2048] [--runs 5] [--suffixes _co _nohq]

For each suffix it writes one noise PNG of size x size, then times core.convert() (the app's own path:
temp file, atomic replace) with converter LOKI and with converter BOHEMIA, `runs` times each, and prints
the medians, the speed ratio and a machine line (CPU model, logical cores, RAM, Windows version; no
host or user names). Needs the real ImageToPAA. The first run of each converter is a warm-up and is
not counted. Noise is the hardest content for the compressor, so real textures are usually as fast or faster.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image

from loki import core


def cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except OSError:
        return platform.processor() or "unknown CPU"


def ram_gb() -> int:
    class Mem(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("tpage", ctypes.c_ulonglong), ("apage", ctypes.c_ulonglong),
                    ("tvirt", ctypes.c_ulonglong), ("avirt", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]

    m = Mem()
    m.length = ctypes.sizeof(Mem)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))  # type: ignore[attr-defined]
    return round(m.total / 2**30)


def machine_line() -> str:
    return f"{cpu_name()}, {os.cpu_count()} logical cores, {ram_gb()} GB RAM, Windows {platform.version()}"


def time_runs(png: Path, tool: Path, converter: str, runs: int) -> list[float]:
    times = []
    for i in range(runs + 1):  # run 0 is the warm-up
        t0 = time.perf_counter()
        res = core.convert(png, tool, None, converter)
        dt = time.perf_counter() - t0
        if not res.ok:
            raise SystemExit(f"{converter} failed on {png.name}: {res.error}")
        if i:
            times.append(dt)
    return times


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--size", type=int, default=2048)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--suffixes", nargs="+", default=["_co", "_nohq"])
    args = ap.parse_args()
    tool = core.find_imagetopaa()
    print(f"machine: {machine_line()}")
    print(f"{args.size}x{args.size} noise RGB, median of {args.runs} runs (after one warm-up)")
    print(f"{'file':<14}{'Loki median':>14}{'ImageToPAA median':>20}{'ratio':>8}")
    with tempfile.TemporaryDirectory(prefix="loki-speed-") as tmp:
        for suffix in args.suffixes:
            png = Path(tmp) / f"bench{suffix}.png"
            Image.effect_noise((args.size, args.size), 80).convert("RGB").save(png)
            loki = statistics.median(time_runs(png, tool, core.LOKI, args.runs))
            bohemia = statistics.median(time_runs(png, tool, core.BOHEMIA, args.runs))
            print(f"{png.name:<14}{loki:>12.2f} s{bohemia:>18.2f} s{bohemia / loki:>7.0f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
