# Loki

Loki turns PNG, TGA and JPG textures into Arma 3 PAA files. Export images from whatever you paint in, drop them on Loki, and it writes each PAA next to its source. Turn on Auto watch and every later save is converted for you.

Created by Astrid Winters.

## Why Loki

- **Any workflow, no plugins.** Save PNG, TGA or JPG the way you always do. No Photoshop, GIMP or Substance plugins, no export presets.
- **Fast.** A built-in converter written in Rust: a 2048x2048 `_co` takes 0.4 s, against 6 s with Bohemia's ImageToPAA. Files convert in parallel.
- **No Arma 3 Tools needed** for any texture type.
- **Clear status.** Every file shows Up to date, Needs converting, or why it was skipped.
- **Command line** for scripts and build steps: `Loki-cli.exe`.

How Loki handles the usual conversion headaches: [Common friction points](docs/USER-GUIDE.md#common-friction-points).

## Download

1. Download the zip from **Releases** and unzip it anywhere. Optionally check it against `SHA256SUMS` (`Get-FileHash` in PowerShell).
2. Run `Loki.exe`. There is no installer.

The exes are not code-signed, so the first time Windows says "Windows protected your PC". Click **More info**, then **Run anyway**.

## Using it

1. Drag PNG, TGA or JPG files from Explorer onto the window. Each gets a row; nothing is written yet.
2. Click **Convert all**, or turn on **Auto watch** for a row to convert it on every save.
3. Optional, per row: **Max size** caps the longest side; **Output size** gives the PAA a fixed size, padded or stretched to fit.

Textures must be powers of two (256x256, 1024x512, ...). Other sizes are flagged unless the row has an output size. Your list and settings are remembered.

Full guide: [docs/USER-GUIDE.md](docs/USER-GUIDE.md)

## Command line

    Loki-cli.exe textures\wall_co.png
    Loki-cli.exe textures --recursive --skip-up-to-date
    Loki-cli.exe "textures\*_co.png" --max-size 1024
    Loki-cli.exe logo_ca.png --size 512x256 --fit pad

In PowerShell, run it as `.\Loki-cli.exe`. Exit codes: 0 all converted, 1 a file was flagged or failed, 2 usage error. All options: `Loki-cli.exe --help` or [docs/CLI.md](docs/CLI.md).

## Known limits

- `_cat`, `_can` and `_non` (noise textures) use Loki's own noise. ImageToPAA's could not be reverse engineered; Loki's is a close approximation, matched in strength, grain and coverage, but not byte for byte. Details: [docs/CONVERTER.md](docs/CONVERTER.md).
- Soft transparency and DXT1 cut-out masks are close matches to ImageToPAA's output rather than byte for byte; they look the same in game. Details: [docs/CONVERTER.md](docs/CONVERTER.md).
- PNGs over 16384 on a side, or over 8192x8192 pixels in all, are too big to load. Anything over 4096 is scaled to 4096 automatically.
- A very noisy 4096x4096 DXT5 texture may not fit the PAA format: set max size to 2048.
- JPG is lossy: use PNG or TGA for normal maps and masks.

## Bugs and ideas

Use **Issues** on this repository. Pull requests are not accepted for now; see `CONTRIBUTING.md`.

## Build from source

Python 3.12 (`pip install pillow pyinstaller`) and a stable Rust toolchain (MSVC). Then `python build/build.py` writes `dist/Loki.exe` and `dist/Loki-cli.exe`, and `python build/release.py` makes the release zip. Tests: `python -m pytest -q`. Details in `CONTRIBUTING.md`; format notes in [docs/PAA-FORMAT.md](docs/PAA-FORMAT.md).

## License

Source-available: MIT plus the Commons Clause. Use, modify and share Loki freely with attribution. Selling Loki itself needs written permission from Astrid Winters. Textures you make with Loki are yours to use or sell, no attribution needed. See `LICENSE` and `THIRD_PARTY_NOTICES`. ImageToPAA and other Bohemia files are not included.

## AI disclosure

AI-assisted coding (Claude, by Anthropic) was used to build Loki.
