# User guide

How to use the Loki window. For the command line see [CLI.md](CLI.md); for converters and suffixes see [CONVERTER.md](CONVERTER.md).

## Adding files

Drag one or more PNG, TGA or JPG files (`.png`, `.tga`, `.jpg`, `.jpeg`, any case) from Explorer onto the Loki window. The PAA is written beside the source with the same stem (`wall_co.tga` gives `wall_co.paa`), and a TGA or JPG gives the same result as a PNG of the same pixels. JPG is lossy, so PNG or TGA is better for normal maps and masks; CMYK JPGs and TGA variants Loki cannot decode are flagged with the reason. If two sources would write the same PAA (`wall_co.png` and `wall_co.tga` in one folder), both rows are flagged naming the other file and neither is converted until you remove one. Each file gets a row, from any folder, mixed as you like. Adding a file only lists it: nothing is written to disk. Dropping a file that is already listed does nothing. Folders and files that are not PNG, TGA or JPG are ignored, and the status bar says how many were skipped.

You can also drop PNG, TGA or JPG files on the `Loki.exe` icon in Explorer: Loki opens with them listed, the same as a drop on the window, and converts nothing until you ask. If Loki is already running, this opens a second window.

## Columns

- **Auto watch**: click the cell (or press Space on selected rows) to turn Auto watch on or off for that PNG. Off by default.
- **Max size**: click the cell and pick Off, 2048, 1024, 512 or 256. It is the longest side allowed in that file's PAA. Bigger images are scaled down with their shape kept, never stretched. Example: 4096x2048 at 1024 becomes 1024x512. Every row has its own, so changing one never changes another. New rows start at Off.
- **Output size**: click the cell to give the PAA a fixed size (like `--size` in Loki-cli). The cell shows Source (off) or, for example, "512x256 contain".
  - Width and height: powers of two from 1 to 4096.
  - Fit: contain keeps the shape and pads (default), pad keeps the pixels and pads, stretch fills.
  - Pad colour and alpha start at the texture type's default. Position: a 3x3 grid, centre by default.
  - Output size and max size exclude each other. A PNG whose sides aren't powers of two converts once it has an output size.
  - With several rows selected, setting one sets them all.

Click a column header to sort the list by it; click again to reverse. An arrow marks the sorted column. The default is File, A to Z, and your choice is remembered. Rows are sorted when you drop or remove files and when you click a header, not while files convert.

## Buttons

- **Convert all**: converts every listed PNG that needs it, in one go, each at its own max size.
- **All on / All off**: turn Auto watch on or off for every listed PNG.
- **Remove**: takes the selected rows off the list (the Delete key does the same). The PNG and its PAA stay on disk, and Loki stops watching that file.
- **About**: version and license.

The list, each file's max size and each file's Auto watch switch are remembered between runs. A listed file that is gone when Loki starts stays in the list as "Missing" and comes back to life if the file returns.

## Auto watch

With a PNG's Auto watch switch on, Loki watches that PNG and converts it to PAA on each save, once it stops changing, at that row's max size. Only listed files are watched. Files changed before you turned it on are not converted by that; use **Convert all** for those.

Loki waits until a saved file has not changed for 1 s, and retries a file another program holds open. The window keeps retrying until it is free (status "Waiting"); the command line waits up to 30 s by default (`--wait-locked`).

## Status column

- **Up to date**: the PAA exists, is newer than the PNG, and has the size the row's max size asks for. It says which converter wrote the PAA last, e.g. "Up to date (Loki)".
- **Needs converting**: no PAA yet, the PNG is newer, or the PAA size no longer matches the row's max size.
- **needs Arma 3 Tools for ...**: this file needs ImageToPAA (see [CONVERTER.md](CONVERTER.md)) and it is not installed. Not converted.
- **Missing**: the PNG is not there (moved, renamed or deleted). It stays listed and recovers when the file returns.
- **Flagged: ...**: Loki will not convert it, and the row says why. For example "Flagged: 300x200: sides must be powers of two (e.g. 256x256) or set an output size" or "Flagged: cannot read: not a valid PNG".
- **Error: ...**: a conversion failed; the row shows the reason in red.

## Power-of-two rule

Arma textures must have a width and height that are powers of two: 1, 2, 4, 8, ... 256, 512, 1024, 2048, 4096. The two sides may differ. 1024x512 and 256x256 are fine. 300x200 and 1000x1000 are flagged and left alone, or set an output size: the command line's `--size` makes a PAA of any PNG size at a power-of-two size you choose (see [CLI.md](CLI.md#output-size)).

## Settings

Stored in `%APPDATA%\Loki\settings.json` (the converter choice, and the file list with each file's max size, output size, Auto watch switch and last converter). Delete the file to reset. If Loki crashes on start, the details are written to `%APPDATA%\Loki\crash.log`.

## Common friction points

| Friction | How Loki handles it |
|---|---|
| A size that is not a power of two fails with an unclear error | The row says "Flagged" with the file's actual size, and Loki does not convert it, unless you set an output size (`--size` on the command line). |
| Conversion fails on every file when Arma 3 Tools is missing, broken or not started from Steam | The built-in converter needs no Arma 3 Tools for most textures. For the rest Loki finds Arma 3 Tools through Steam itself; the Browse... button or `--imagetopaa` is only for unusual installs. |
| Batch conversion is slow | The built-in converter is fast (see Speed in [CONVERTER.md](CONVERTER.md#speed)), and **Convert all** and `--jobs` convert files in parallel. |
| A manual convert step after every export, or plugins that need a hard-coded tools path | Drop the PNGs in once and turn on Auto watch. Each later save is converted. No plugins. |
| Exports still being written, or files held open by another program | With Auto watch on, Loki waits until a saved file has not changed for 1 s, and retries a file another program holds open. |
| Not knowing which PAAs are out of date | The status column shows "Up to date" or "Needs converting" for every file. |

## Headless use

`python -m loki --scenario FILE --settings JSON [--out LOG]` runs a script of actions and prints the state; `--screenshot OUT.png` captures the window (add `--drop FILE...` to post it a real file drop first).
