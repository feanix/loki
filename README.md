# Loki

Created by Astrid Winters.

Loki turns PNG textures into Arma 3 PAA files. Export your PNGs from whatever you paint in, drop them on Loki, and Loki makes the PAAs.

Two things make it different, and both are headline features:

- **A built-in converter written in Rust.** Loki has its own PAA encoder inside `Loki.exe`. It is fast, and for the texture types it supports (see [The built-in converter](#the-built-in-converter)) it needs nothing else installed: no Arma 3 Tools, no Python, no Rust.
- **A command-line converter, `Loki-cli.exe`**, for scripts, build pipelines and batch jobs. It converts and exits. See [Command line](#command-line).

## Why Loki

**Convenience.** Loki works with any texture workflow. There are no plugins for Photoshop, GIMP, Substance or anything else to install, and no export presets to learn. Save a PNG the way you always do; Loki makes the PAA beside it. Turn on *bake* for a file and its PAA is rebuilt every time you save the PNG. After that you can forget about it.

**Speed.** Bohemia's ImageToPAA is slow for big textures. Loki's own converter is not. Measured with `build/bench_speed.py`, converting one 2048x2048 noise texture through Loki's own code path, median of 5 runs after a warm-up, on an Intel Core Ultra 7 265 (20 logical cores), 63 GB RAM, Windows 11 (10.0.26200):

| file | Loki converter | Bohemia ImageToPAA | faster by |
|---|---|---|---|
| `bench_co.png` (2048x2048) | 0.40 s | 5.98 s | 15x |
| `bench_nohq.png` (2048x2048) | 0.81 s | 1.92 s | 2x |

Other machines differ; run the benchmark yourself (it needs Arma 3 Tools). In practice a saved texture is converted in a fraction of a second to a second, normally before you have switched windows to check.

**Source-available.** Loki is free to use, modify and share, with attribution. See [License](#license) for the exact terms: selling Loki itself needs written permission, and textures you make with it are yours to use or sell freely.

## Common friction points

| Friction | How Loki handles it |
|---|---|
| A size that is not a power of two fails with an unclear error | The row says "Flagged" with the file's actual size, and Loki does not convert it. |
| Conversion fails on every file when Arma 3 Tools is missing, broken or not started from Steam | The built-in converter needs no Arma 3 Tools for most textures. For the rest Loki finds Arma 3 Tools through Steam itself; the Browse... button or `--imagetopaa` is only for unusual installs. |
| Batch conversion is slow | The built-in converter is fast (see Speed above), and **Convert all** and `--jobs` convert files in parallel. |
| A manual convert step after every export, or plugins that need a hard-coded tools path | Drop the PNGs in once and turn on bake. Each later save is converted. No plugins. |
| Exports still being written, or files held open by another program | With bake on, Loki waits until a saved file has not changed for 1 s, and retries a file another program holds open. The window keeps retrying until it is free (status "Waiting"); the command line waits up to 30 s by default (`--wait-locked`). |
| Not knowing which PAAs are out of date | The status column shows "Up to date" or "Needs converting" for every file. |

## Download and first run

1. Download `Loki-0.3.0-win64.zip` from the Releases page of this repository, and unzip it anywhere. Optionally check it against `SHA256SUMS` (`Get-FileHash Loki-0.3.0-win64.zip` in PowerShell).
2. Run `Loki.exe`. There is no installer.

The zip holds `Loki.exe` (the window), `Loki-cli.exe` (command line), this README, `LICENSE` and `THIRD_PARTY_NOTICES`.

**Windows will warn you the first time.** The files are not code-signed, so Windows SmartScreen says "Windows protected your PC" and names an unknown publisher. Click **More info**, then **Run anyway**. The source in this repository is what the exes are built from (see [Build from source](#build-from-source)).

## Using the window

Drag one or more PNG files from Explorer onto the Loki window. Each file gets a row, from any folder, mixed as you like. Adding a file only lists it: nothing is written to disk. Dropping a file that is already listed does nothing. Folders and files that are not PNG are ignored, and the status bar says how many were skipped.

Each row has:

- **Bake**: click the cell (or press Space on selected rows) to turn bake-on-save on or off for that PNG. Off by default.
- **Max size**: click the cell and pick Off, 2048, 1024, 512 or 256. It is the longest side allowed in that file's PAA. Bigger images are scaled down with their shape kept, never stretched. Example: 4096x2048 at 1024 becomes 1024x512. Every row has its own, so changing one never changes another. New rows start at Off.

Buttons:

- **Convert all**: converts every listed PNG that needs it, in one go, each at its own max size.
- **All on / All off**: turn bake-on-save on or off for every listed PNG.
- **Remove**: takes the selected rows off the list (the Delete key does the same). The PNG and its PAA stay on disk, and Loki stops watching that file.
- **About**: version and license.

The list, each file's max size and each file's bake switch are remembered between runs. A listed file that is gone when Loki starts stays in the list as "Missing" and comes back to life if the file returns.

### Bake on change

With a PNG's Bake switch on, Loki converts it on the next save after it stops changing, at that row's max size. Only listed files are watched. Files changed before you turned it on are not converted by that; use **Convert all** for those.

### Status column

- **Up to date**: the PAA exists, is newer than the PNG, and has the size the row's max size asks for. It says which converter wrote the PAA last, e.g. "Up to date (Loki)".
- **Needs converting**: no PAA yet, the PNG is newer, or the PAA size no longer matches the row's max size.
- **needs Arma 3 Tools for ...**: this file needs ImageToPAA (see below) and it is not installed. Not converted.
- **Missing**: the PNG is not there (moved, renamed or deleted). It stays listed and recovers when the file returns.
- **Flagged: ...**: Loki will not convert it, and the row says why. For example "Flagged: 300x200: sides must be powers of two (e.g. 256x256)" or "Flagged: cannot read: not a valid PNG".
- **Error: ...**: a conversion failed; the row shows the reason in red.

### Power-of-two rule

Arma textures must have a width and height that are powers of two: 1, 2, 4, 8, ... 256, 512, 1024, 2048, 4096. The two sides may differ. 1024x512 and 256x256 are fine. 300x200 and 1000x1000 are flagged and left alone.

### Settings

Stored in `%APPDATA%\Loki\settings.json` (the converter choice, and the file list with each file's max size, bake switch and last converter). Delete the file to reset. If Loki crashes on start, the details are written to `%APPDATA%\Loki\crash.log`.

## The built-in converter

The dropdown at the top of the window picks the converter, and the choice is remembered.

- **Loki converter** (default): Loki's own encoder, written in Rust and built into `Loki.exe`. PNGs up to 4096 pixels per side.
- **Bohemia ImageToPAA**: everything goes through Arma 3 Tools' `ImageToPAA.exe` for every file.

Which converter handles a file with the Loki converter selected is decided by the file name suffix (the part after the last underscore), the same way Bohemia's `TexConvert.cfg` picks a texture type:

- **Loki converter**: a name with no suffix, or any of `_co _ca _cdt _lco _mco _lca _mask _mc _pr _draftlco _detail _dt _dtsmdi _as _ads _adshq _sm _smdi _no _nohq _novhq _nofhq _nshq _nopx _nof _ns _normalmap _noex _nofex _nsex _raw _1555 _4444 _88 _8888 _gs _dxt1 _dxt5`. For example `rock_co.png`, `rock_nohq.png`, `rock_smdi.png`, `rock.png`. The authoritative list is `_paa.supported` in the encoder; it is checked against ImageToPAA's output per suffix.
- **ImageToPAA** (automatic, needs Arma 3 Tools): `_sky`, the noise suffixes `_cat`, `_can` and `_non`, any other suffix (a name like `my_rock.png` counts as suffix `_rock`), and any PNG over 4096 on a side.

Arma 3 Tools (free on Steam, in the Tools section of your library) is optional. ImageToPAA is not bundled with Loki. Loki finds it automatically; if it cannot and something needs it, a "Browse..." button appears so you can point it at `ImageToPAA.exe` yourself. Without Arma 3 Tools nothing warns you until a row needs it (or Bohemia is selected); that row says "needs Arma 3 Tools for _sky" (or "over 4096: needs Arma 3 Tools") and is not converted.

Switching the converter never marks existing PAAs as out of date: a row is "Up to date" or "Needs converting" from the PAA's size and age alone.

### Known limits (0.3.0)

For a few kinds of picture the Loki converter cannot yet reproduce ImageToPAA's output exactly, so it refuses the file and Loki hands it to ImageToPAA instead. With Arma 3 Tools installed this happens automatically and you notice nothing but the speed. Without it the row says "needs Arma 3 Tools (soft alpha)" or "(1-bit alpha)". The cases:

- **Soft alpha**: textures with smooth transparency, for example glass or foliage in `_ca`.
- **DXT1 cut-out alpha**: `DXT1` types that carry a 1-bit transparency mask, for example hair cards.
- **`_sky`, `_cat`, `_can`, `_non`**: always ImageToPAA (the noise suffixes use Bohemia's own noise generator).
- **4096x4096 with very noisy detail as DXT5** (`_ca`, `_nohq` and similar): the data does not fit the PAA format's size field, so the row says "too detailed to fit at 4096 as DXT5: set max size 2048". Set max size to 2048 and it converts.

These are planned for 0.3.1 where possible.

## Command line

`Loki-cli.exe` (next to `Loki.exe`) converts PNGs without the window and exits, so it fits scripts, build steps and batch jobs. `Loki.exe` itself stays windowed: `Loki.exe convert ...` only prints a pointer to `Loki-cli.exe` and exits with 2. The command line does not read or change the window's settings or file list, and has no watch mode.

    Loki-cli.exe convert textures\wall_co.png
    Loki-cli.exe convert textures\*_co.png --max-size 1024
    Loki-cli.exe convert textures --recursive --skip-up-to-date --jobs 4
    Loki-cli.exe convert rock_sky.png --imagetopaa "D:\Games\Arma 3 Tools\ImageToPAA\ImageToPAA.exe"
    Loki-cli.exe --help
    Loki-cli.exe --version

Inputs are PNG files, glob patterns (Loki expands them itself, since cmd and PowerShell do not) and folders (their `*.png`; add `--recursive` for subfolders). Each PAA is written beside its PNG, with the same routing as the window: the Loki converter, ImageToPAA for the files listed above, "needs Arma 3 Tools" when it is absent, non-power-of-two PNGs flagged and not converted. Every given file is converted unless you pass `--skip-up-to-date`.

Options: `--max-size off|2048|1024|512|256` (default off), `--imagetopaa PATH` (see below), `--jobs N` (parallel conversions), `--skip-up-to-date`, `--recursive`, `--wait-locked SECONDS` (default 30: how long to wait for a PNG or PAA another program holds open, then `error ...: PAA in use`), `--quiet` (only problems and the summary).

The built-in converter does most files. These go to Bohemia's ImageToPAA automatically: some textures with transparency (soft alpha, or cut-out alpha such as hair cards), `_sky`, `_cat`, `_can`, `_non`, unknown suffixes, and PNGs over 4096 on a side. `--imagetopaa PATH` is only needed if Arma 3 Tools is installed somewhere Loki cannot find; normally Loki finds it through Steam. A bad path is a usage error (exit 2). If a file needs ImageToPAA and it is not found, that file is reported as "needs Arma 3 Tools" and the exit code is 1.

One line per file, then a summary. Files are converted in parallel, so lines can come in any order. A real run over `wall_co.png`, `odd_co.png` (300x200), `rock_sky.png`, `rock_nohq.png` and a file that does not exist:

    flagged    odd_co.png: 300x200: sides must be powers of two (e.g. 256x256)
    error      x.png: file not found
    converted  wall_co.png -> wall_co.paa (256x256, loki, 0.02 s)
    converted  rock_nohq.png -> rock_nohq.paa (256x256, loki, 0.02 s)
    converted  rock_sky.png -> rock_sky.paa (256x256, bohemia, 0.14 s)
    done: 3 converted, 0 skipped, 1 flagged, 0 need Arma 3 Tools, 1 errors (0.2 s)

On a machine without Arma 3 Tools the `_sky` file prints instead:

    needs-tool rock_sky.png: needs Arma 3 Tools for _sky

Exit code 0: everything converted or skipped as up to date. 1: any file flagged, needing Arma 3 Tools, or failed. 2: usage error (bad option, no PNG matched). In PowerShell 5.1, `> file` writes UTF-16; use `| Out-File -Encoding utf8 file` for UTF-8.

## Bugs and ideas

Loki takes bugs and ideas through **Issues** on this repository. Pull requests are not accepted for now. See `CONTRIBUTING.md`.

## Build from source

Python 3.12, `pip install pillow pyinstaller`, and a Rust toolchain (`cargo`, stable, MSVC target). The built-in encoder is a Rust crate (`native/paa`) built as a Python extension. `build/build_native.py` runs `cargo build --release --locked --features python` and copies the result to `src/loki/_paa.pyd`; no maturin is needed. Then:

    python build/build.py

This builds the encoder, then writes `dist/Loki.exe` and the console `dist/Loki-cli.exe` (one file each; the encoder is inside it, so the exe needs no Rust, Python or Arma 3 Tools on the machine). To build the release zip (`dist/Loki-<version>-win64.zip` and `dist/SHA256SUMS`) run `python build/release.py`. Cargo output goes to a folder per checkout, `%LOCALAPPDATA%\Loki\cargo-target\<hash>` (or `CARGO_TARGET_DIR` if set). Running from source also needs the extension: `python build/build_native.py` once (the tests do it themselves). Run from source with `python -m loki` (with `src` on the path). Tests: `python -m pytest -q`. The icon is generated by `python assets/make_icon.py`. The PAA format notes behind the encoder are in `docs/PAA-FORMAT.md`.

Headless use: `python -m loki --scenario FILE --settings JSON [--out LOG]` runs a script of actions and prints the state; `--screenshot OUT.png` captures the window (add `--drop FILE...` to post it a real file drop first).

## License

Loki is source-available (free to use, open code), not open source: MIT plus the Commons Clause, Copyright (c) 2026 Astrid Winters. In plain words: you are free to use, modify and share Loki with attribution (keep the copyright notice). Selling Loki itself, or a product built mainly on it, needs written permission from Astrid Winters. Textures you make with Loki are yours: use or sell them freely, including in paid games and mods, with no permission or attribution needed. See `LICENSE`. Licences of the software bundled in `Loki.exe` are in `THIRD_PARTY_NOTICES`. ImageToPAA and other Bohemia Interactive files are not part of Loki and are not included.

## AI disclosure

AI-assisted coding (Claude, by Anthropic) was used to build Loki.
