# Loki

Created by Astrid Winters.

Loki turns PNG textures into Arma 3 PAA files. Export your PNGs from whatever you paint in, drop them on Loki, and Loki makes the PAAs.

Two things make it different, and both are headline features:

- **A built-in converter written in Rust.** Loki has its own PAA encoder inside `Loki.exe`. It is fast, and for the texture types it supports (see [The built-in converter](#the-built-in-converter)) it needs nothing else installed: no Arma 3 Tools, no Python, no Rust.
- **A command-line converter, `Loki-cli.exe`**, for scripts, build pipelines and batch jobs. It converts and exits. See [Command line](#command-line).

## Why Loki

**Convenience.** Loki works with any texture workflow. There are no plugins for Photoshop, GIMP, Substance or anything else to install, and no export presets to learn. Save a PNG the way you always do; Loki makes the PAA beside it. Turn on *Auto watch* for a file and its PAA is rebuilt every time you save the PNG. After that you can forget about it.

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
| A size that is not a power of two fails with an unclear error | The row says "Flagged" with the file's actual size, and Loki does not convert it, unless you set an output size (`--size` on the command line). |
| Conversion fails on every file when Arma 3 Tools is missing, broken or not started from Steam | The built-in converter needs no Arma 3 Tools for most textures. For the rest Loki finds Arma 3 Tools through Steam itself; the Browse... button or `--imagetopaa` is only for unusual installs. |
| Batch conversion is slow | The built-in converter is fast (see Speed above), and **Convert all** and `--jobs` convert files in parallel. |
| A manual convert step after every export, or plugins that need a hard-coded tools path | Drop the PNGs in once and turn on Auto watch. Each later save is converted. No plugins. |
| Exports still being written, or files held open by another program | With Auto watch on, Loki waits until a saved file has not changed for 1 s, and retries a file another program holds open. The window keeps retrying until it is free (status "Waiting"); the command line waits up to 30 s by default (`--wait-locked`). |
| Not knowing which PAAs are out of date | The status column shows "Up to date" or "Needs converting" for every file. |

## Download and first run

1. Download `Loki-0.3.1-win64.zip` from the Releases page of this repository, and unzip it anywhere. Optionally check it against `SHA256SUMS` (`Get-FileHash Loki-0.3.1-win64.zip` in PowerShell).
2. Run `Loki.exe`. There is no installer.

The zip holds `Loki.exe` (the window), `Loki-cli.exe` (command line), this README, `LICENSE` and `THIRD_PARTY_NOTICES`.

**Windows will warn you the first time.** The files are not code-signed, so Windows SmartScreen says "Windows protected your PC" and names an unknown publisher. Click **More info**, then **Run anyway**. The source in this repository is what the exes are built from (see [Build from source](#build-from-source)).

## Using the window

Drag one or more PNG files from Explorer onto the Loki window. Each file gets a row, from any folder, mixed as you like. Adding a file only lists it: nothing is written to disk. Dropping a file that is already listed does nothing. Folders and files that are not PNG are ignored, and the status bar says how many were skipped.

Each row has:

- **Auto watch**: click the cell (or press Space on selected rows) to turn Auto watch on or off for that PNG. Off by default.
- **Max size**: click the cell and pick Off, 2048, 1024, 512 or 256. It is the longest side allowed in that file's PAA. Bigger images are scaled down with their shape kept, never stretched. Example: 4096x2048 at 1024 becomes 1024x512. Every row has its own, so changing one never changes another. New rows start at Off.
- **Output size**: click the cell to give the PAA a fixed size (like `--size` in Loki-cli). The cell shows Source (off) or, for example, "512x256 contain".
  - Width and height: powers of two from 1 to 4096.
  - Fit: contain keeps the shape and pads (default), pad keeps the pixels and pads, stretch fills.
  - Pad colour and alpha start at the texture type's default. Position: a 3x3 grid, centre by default.
  - Output size and max size exclude each other. A PNG whose sides aren't powers of two converts once it has an output size.
  - With several rows selected, setting one sets them all.

Click a column header to sort the list by it; click again to reverse. An arrow marks the sorted column. The default is File, A to Z, and your choice is remembered. Rows are sorted when you drop or remove files and when you click a header, not while files convert.

Buttons:

- **Convert all**: converts every listed PNG that needs it, in one go, each at its own max size.
- **All on / All off**: turn Auto watch on or off for every listed PNG.
- **Remove**: takes the selected rows off the list (the Delete key does the same). The PNG and its PAA stay on disk, and Loki stops watching that file.
- **About**: version and license.

The list, each file's max size and each file's Auto watch switch are remembered between runs. A listed file that is gone when Loki starts stays in the list as "Missing" and comes back to life if the file returns.

### Auto watch

With a PNG's Auto watch switch on, Loki watches that PNG and converts it to PAA on each save, once it stops changing, at that row's max size. Only listed files are watched. Files changed before you turned it on are not converted by that; use **Convert all** for those.

### Status column

- **Up to date**: the PAA exists, is newer than the PNG, and has the size the row's max size asks for. It says which converter wrote the PAA last, e.g. "Up to date (Loki)".
- **Needs converting**: no PAA yet, the PNG is newer, or the PAA size no longer matches the row's max size.
- **needs Arma 3 Tools for ...**: this file needs ImageToPAA (see below) and it is not installed. Not converted.
- **Missing**: the PNG is not there (moved, renamed or deleted). It stays listed and recovers when the file returns.
- **Flagged: ...**: Loki will not convert it, and the row says why. For example "Flagged: 300x200: sides must be powers of two (e.g. 256x256) or set an output size" or "Flagged: cannot read: not a valid PNG".
- **Error: ...**: a conversion failed; the row shows the reason in red.

### Power-of-two rule

Arma textures must have a width and height that are powers of two: 1, 2, 4, 8, ... 256, 512, 1024, 2048, 4096. The two sides may differ. 1024x512 and 256x256 are fine. 300x200 and 1000x1000 are flagged and left alone, or set an output size: the command line's `--size` makes a PAA of any PNG size at a power-of-two size you choose (see Command line).

### Settings

Stored in `%APPDATA%\Loki\settings.json` (the converter choice, and the file list with each file's max size, output size, Auto watch switch and last converter). Delete the file to reset. If Loki crashes on start, the details are written to `%APPDATA%\Loki\crash.log`.

## The built-in converter

The dropdown at the top of the window picks the converter, and the choice is remembered.

- **Loki converter** (default): Loki's own encoder, written in Rust and built into `Loki.exe`. A PNG bigger than 4096 on a side (up to 16384, and 8192x8192 pixels in all) is scaled down to 4096 first, shape kept, the way ImageToPAA does; Max size still applies on top.
- **Bohemia ImageToPAA**: everything goes through Arma 3 Tools' `ImageToPAA.exe` for every file.

Which converter handles a file with the Loki converter selected is decided by the file name suffix (the part after the last underscore), the same way Bohemia's `TexConvert.cfg` picks a texture type:

- **Loki converter**: a name with no suffix, a suffix Bohemia's `TexConvert.cfg` does not name (it gets the same default rule as no suffix, so `my_rock.png` and `rock_foo.png` are encoded by Loki), or any of `_co _ca _cdt _lco _mco _lca _mask _mc _pr _draftlco _detail _dt _dtsmdi _as _ads _adshq _sm _smdi _no _nohq _novhq _nofhq _nshq _nopx _nof _ns _normalmap _noex _nofex _nsex _raw _1555 _4444 _88 _8888 _gs _dxt1 _dxt5 _sky`. For example `rock_co.png`, `rock_nohq.png`, `rock_smdi.png`, `rock.png`. The authoritative list is `_paa.supported` in the encoder; it is checked against ImageToPAA's output per suffix.
- **ImageToPAA** (automatic, needs Arma 3 Tools): the noise suffixes `_cat`, `_can` and `_non`.

Arma 3 Tools (free on Steam, in the Tools section of your library) is optional. ImageToPAA is not bundled with Loki. Loki finds it automatically; if it cannot and something needs it, a "Browse..." button appears so you can point it at `ImageToPAA.exe` yourself. Without Arma 3 Tools nothing warns you until a row needs it (or Bohemia is selected); that row says "needs Arma 3 Tools for _cat" and is not converted.

Switching the converter never marks existing PAAs as out of date: a row is "Up to date" or "Needs converting" from the PAA's size and age alone.

### Known limits (0.3.1)

The Loki converter writes every texture type itself except the noise suffixes `_cat`, `_can` and `_non` (they use Bohemia's own noise generator, so those go to ImageToPAA). Two kinds of transparency are not byte for byte what ImageToPAA writes. Both were measured against it, and Loki does not hand either to ImageToPAA any more:

- **Soft alpha** (smooth transparency, for example glass or foliage in `_ca`, or any unsuffixed file with soft alpha): the colour that ImageToPAA stores as the texture's average (the AVGC tag, which the game uses for far away views) follows an alpha-dependent rule it does not document. Loki reproduces it from a table measured on ImageToPAA's own output: within 1 level of 255 on every one of 57 files tried (49 exact), owner art included. The mip chain is identical to ImageToPAA's; the DXT5 alpha blocks of detailed soft art can be up to about 2 dB less exact than ImageToPAA's (both sit near 40 dB from the source, which is not visible).
- **DXT1 cut-out alpha** (a `DXT1` type with a 1-bit transparency mask, for example hair cards in `_co`): ImageToPAA dithers the mask. Its rule could not be reverse engineered: a flat alpha is not dithered (opaque from alpha 4 up), a ramp gives a regular 8 texel pattern, and the same 4x4 block is masked in up to three ways depending on its neighbours. Loki cuts its own mask instead: texels of a block of one alpha are opaque from alpha 4, every other texel by one threshold per mip, chosen so that the share of opaque texels equals the mean alpha of that mip (a plain cut at 128 on art whose alpha is mostly 0 or 255). Checked against ImageToPAA on an owner hair card (1024x1024): 99.65 percent of the texels of mip 0 have the same bit, and the opaque share stays within 0.7 percentage points of ImageToPAA's on every mip down to 16x16 (4 points at worst on a large binary cut-out). Owner in-game check (decision 56), Loki beside ImageToPAA on panels side by side: the owner hair card and a large owner cut-out looked the same, and a generated hair card looked slightly better with Loki ("so similar that differences are hard to find").
- **PNGs over 16384 on a side, or over 8192x8192 pixels in all**: too big to load. Scale it to 8192 or less in your image editor; Loki then scales it to 4096 itself. The row says so and nothing is converted until you do.
- **4096x4096 with very noisy detail as DXT5** (`_ca`, `_nohq` and similar; also a bigger PNG that is scaled down to 4096): the data does not fit the PAA format's size field, so the row says "too detailed to fit at 4096 as DXT5: set max size 2048". Set max size to 2048 and it converts.


## Command line

`Loki-cli.exe` (next to `Loki.exe`) converts PNGs without the window and exits, so it fits scripts, build steps and batch jobs. `Loki.exe` itself stays windowed: `Loki.exe <inputs...>` only prints a pointer to `Loki-cli.exe` and exits with 2. The command line does not read or change the window's settings or file list, and has no watch mode. In PowerShell, run it from its folder as `.\Loki-cli.exe` (or add the folder to PATH).

    Loki-cli.exe textures\wall_co.png
    Loki-cli.exe textures\*_co.png --max-size 1024
    Loki-cli.exe textures --recursive --skip-up-to-date --jobs 4
    Loki-cli.exe textures\door_co.png --size 512x256 --fit pad
    Loki-cli.exe rock_cat.png --imagetopaa "D:\Games\Arma 3 Tools\ImageToPAA\ImageToPAA.exe"
    Loki-cli.exe --help
    Loki-cli.exe --version

Converting is the default action. The 0.3.0 form `Loki-cli.exe convert <inputs...>` still works; only the bare first word `convert` means the command, so to convert a file or folder literally named `convert`, write `.\convert` (or any other path form).

Inputs are PNG files, glob patterns (Loki expands them itself, since cmd and PowerShell do not) and folders (their `*.png`; add `--recursive` for subfolders). Each PAA is written beside its PNG, with the same routing as the window: the Loki converter, ImageToPAA for the files listed above, "needs Arma 3 Tools" when it is absent, non-power-of-two PNGs flagged and not converted (unless `--size` is given, see Output size). Every given file is converted unless you pass `--skip-up-to-date`.

Options: `--max-size off|2048|1024|512|256` (default off), `--size WxH` with `--fit`, `--pad-colour` and `--pad-align` (see Output size), `--imagetopaa PATH` (see below), `--jobs N` (parallel conversions), `--skip-up-to-date`, `--recursive`, `--wait-locked SECONDS` (default 30: how long to wait for a PNG or PAA another program holds open, then `error ...: PAA in use`), `--quiet` (only problems and the summary).

The built-in converter does most files. These go to Bohemia's ImageToPAA automatically: `_cat`, `_can`, `_non`. `--imagetopaa PATH` is only needed if Arma 3 Tools is installed somewhere Loki cannot find; normally Loki finds it through Steam. A bad path is a usage error (exit 2). If a file needs ImageToPAA and it is not found, that file is reported as "needs Arma 3 Tools" and the exit code is 1.

### Output size

By default a PAA has the size of its PNG (capped by `--max-size`). `--size WxH` sets the size of the PAA instead, and then the PNG may have any size: both sides of the output must be powers of two from 1 to 4096, and the PNG is made to fit. `--size` and `--max-size` cannot be used together (usage error, exit 2). The options `--fit`, `--pad-colour` and `--pad-align` only work with `--size`.

- `--fit contain` (default): scale the PNG, shape kept, until it fits inside the size, and fill the rest. `--fit stretch`: scale to exactly the size (the shape changes). `--fit pad`: no scaling; the PNG is placed on a canvas of the size and the rest is filled. A PNG larger than the size is flagged with a clear reason (exit 1), never resized.
- `--pad-colour COLOUR`: the fill, as `#RRGGBB`, `#RRGGBBAA` or `R,G,B,A` (0-255 each); `--pad-color` also works. It is applied to the picture before any channel swizzle of the texture type, so it is the colour you would see in the PNG. Default by texture type: normal maps (`_no`, `_nohq`, `_novhq`, `_nofhq`, `_nshq`, `_non`, `_nopx`, `_ns`, `_nof`, `_normalmap` and the like) `128,128,255,255` (a flat normal); ambient shadow (`_as`, `_ads`, `_adshq`) `255,255,255,255`; types that keep alpha (`_ca`, `_lca`, `_cat`, `_can`, `_8888` and other types whose format has an alpha channel) `0,0,0,0`; everything else `0,0,0,255`.
- `--pad-align ALIGN`: where the image sits when it does not fill the size: `centre` (default; `center` also works), `top-left`, `top`, `top-right`, `left`, `right`, `bottom-left`, `bottom`, `bottom-right`.

Scaling uses a Lanczos filter on premultiplied alpha, so a transparent edge gets no dark or colour halo, and normal maps are renormalised to unit length afterwards. A scale that is an exact halving (1024x1024 to 512x512) uses the texture type's own mip filter, so the result is the mip the normal path would store. The source PNG is never modified. Files that ImageToPAA converts get the same sized picture (as an 8-bit copy), so both converters agree.

Loki remembers the output settings it used in `%APPDATA%\Loki\outputs.json`; with `--skip-up-to-date`, a PAA made with other size, fit, colour or alignment settings (or by a run without `--size`) is converted again, and deleting that file only means the next `--skip-up-to-date` run converts again.

One line per file, then a summary. Files are converted in parallel, so lines can come in any order. A real run over `wall_co.png`, `odd_co.png` (300x200), `rock_cat.png`, `rock_nohq.png` and a file that does not exist:

    flagged    odd_co.png: 300x200: sides must be powers of two (e.g. 256x256) or set an output size
    error      x.png: file not found
    converted  wall_co.png -> wall_co.paa (256x256, loki, 0.02 s)
    converted  rock_nohq.png -> rock_nohq.paa (256x256, loki, 0.03 s)
    converted  rock_cat.png -> rock_cat.paa (256x256, bohemia, 0.55 s)
    done: 3 converted, 0 skipped, 1 flagged, 0 need Arma 3 Tools, 1 errors (0.6 s)

On a machine without Arma 3 Tools the `_cat` file prints instead:

    needs-tool rock_cat.png: needs Arma 3 Tools for _cat

Exit codes (the same list is at the end of `Loki-cli.exe --help`): 0 all files converted or up to date. 1 any file flagged, needing Arma 3 Tools, or failed. 2 usage error (bad option, no PNG matched). In PowerShell 5.1, `> file` writes UTF-16; use `| Out-File -Encoding utf8 file` for UTF-8.

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
