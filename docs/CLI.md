# Command line

`Loki-cli.exe` (next to `Loki.exe`) converts PNG, TGA and JPG pictures without the window and exits, so it fits scripts, build steps and batch jobs. `Loki.exe` itself stays windowed: `Loki.exe convert ...` only prints a pointer to `Loki-cli.exe` and exits with 2 (any other file arguments open the window with those files listed). The command line does not read or change the window's settings or file list, and has no watch mode. In PowerShell, run it from its folder as `.\Loki-cli.exe` (or add the folder to PATH).

    Loki-cli.exe textures\wall_co.png
    Loki-cli.exe textures\*_co.png --max-size 1024
    Loki-cli.exe textures --recursive --skip-up-to-date --jobs 4
    Loki-cli.exe textures\door_co.png --size 512x256 --fit pad
    Loki-cli.exe textures\wall_co.png --imagetopaa "D:\Games\Arma 3 Tools\ImageToPAA\ImageToPAA.exe"
    Loki-cli.exe --help
    Loki-cli.exe --version

Converting is the default action. The 0.3.0 form `Loki-cli.exe convert <inputs...>` still works; only the bare first word `convert` means the command, so to convert a file or folder literally named `convert`, write `.\convert` (or any other path form).

Inputs are PNG, TGA and JPG files (`.png`, `.tga`, `.jpg`, `.jpeg`, any case), glob patterns (Loki expands them itself, since cmd and PowerShell do not) and folders (their `*.png`, `*.tga`, `*.jpg` and `*.jpeg`; add `--recursive` for subfolders). Each PAA is written beside its source with the same stem (`wall_co.tga` gives `wall_co.paa`), and the stem decides the texture type exactly as for a PNG. The result for a TGA or JPG equals the result for a PNG of the same pixels. If two sources would write the same PAA (`wall_co.png` and `wall_co.tga` in one folder), both are flagged with a message naming the other file and neither is converted until one is removed. JPG is lossy: use PNG or TGA for normal maps and masks. Loki refuses CMYK JPGs and TGA variants it cannot decode, with a message saying why. Routing is the same as the window: the Loki converter writes every texture type (the noise suffixes `_cat`, `_can` and `_non` included, see [CONVERTER.md](CONVERTER.md); no suffix goes to ImageToPAA now), "needs Arma 3 Tools" for a file that only ImageToPAA could write when it is absent, non-power-of-two PNGs flagged and not converted (unless `--size` is given, see [Output size](#output-size)). Every given file is converted unless you pass `--skip-up-to-date`.

## Options

`--max-size off|2048|1024|512|256` (default off), `--size WxH` with `--fit`, `--pad-colour` and `--pad-align` (see [Output size](#output-size)), `--imagetopaa PATH` (see below), `--jobs N` (parallel conversions), `--skip-up-to-date`, `--recursive`, `--wait-locked SECONDS` (default 30: how long to wait for a PNG or PAA another program holds open, then `error ...: PAA in use`), `--quiet` (only problems and the summary).

The built-in converter writes every texture type itself; no suffix goes to Bohemia's ImageToPAA automatically now. `--imagetopaa PATH` is only needed if a file ever needs ImageToPAA and Arma 3 Tools is installed somewhere Loki cannot find; normally Loki finds it through Steam. A bad path is a usage error (exit 2). If a file needs ImageToPAA and it is not found, that file is reported as "needs Arma 3 Tools" and the exit code is 1.

## Output size

By default a PAA has the size of its PNG (capped by `--max-size`). `--size WxH` sets the size of the PAA instead, and then the PNG may have any size: both sides of the output must be powers of two from 1 to 4096, and the PNG is made to fit. `--size` and `--max-size` cannot be used together (usage error, exit 2). The options `--fit`, `--pad-colour` and `--pad-align` only work with `--size`.

- `--fit contain` (default): scale the PNG, shape kept, until it fits inside the size, and fill the rest. `--fit stretch`: scale to exactly the size (the shape changes). `--fit pad`: no scaling; the PNG is placed on a canvas of the size and the rest is filled. A PNG larger than the size is flagged with a clear reason (exit 1), never resized.
- `--pad-colour COLOUR`: the fill, as `#RRGGBB`, `#RRGGBBAA` or `R,G,B,A` (0-255 each); `--pad-color` also works. It is applied to the picture before any channel swizzle of the texture type, so it is the colour you would see in the PNG. Default by texture type: normal maps (`_no`, `_nohq`, `_novhq`, `_nofhq`, `_nshq`, `_non`, `_nopx`, `_ns`, `_nof`, `_normalmap` and the like) `128,128,255,255` (a flat normal); ambient shadow (`_as`, `_ads`, `_adshq`) `255,255,255,255`; types that keep alpha (`_ca`, `_lca`, `_cat`, `_can`, `_8888` and other types whose format has an alpha channel) `0,0,0,0`; everything else `0,0,0,255`.
- `--pad-align ALIGN`: where the image sits when it does not fill the size: `centre` (default; `center` also works), `top-left`, `top`, `top-right`, `left`, `right`, `bottom-left`, `bottom`, `bottom-right`.

Scaling uses a Lanczos filter on premultiplied alpha, so a transparent edge gets no dark or colour halo, and normal maps are renormalised to unit length afterwards. A scale that is an exact halving (1024x1024 to 512x512) uses the texture type's own mip filter, so the result is the mip the normal path would store. The source PNG is never modified. Files that ImageToPAA converts get the same sized picture (as an 8-bit copy), so both converters agree.

Loki remembers the output settings it used in `%APPDATA%\Loki\outputs.json`; with `--skip-up-to-date`, a PAA made with other size, fit, colour or alignment settings (or by a run without `--size`) is converted again, and deleting that file only means the next `--skip-up-to-date` run converts again.

## Output

One line per file, then a summary. Files are converted in parallel, so lines can come in any order. A real run over `wall_co.png`, `odd_co.png` (300x200), `rock_cat.png`, `rock_nohq.png` and a file that does not exist:

    flagged    odd_co.png: 300x200: sides must be powers of two (e.g. 256x256) or set an output size
    error      x.png: file not found
    converted  wall_co.png -> wall_co.paa (256x256, loki, 0.02 s)
    converted  rock_cat.png -> rock_cat.paa (256x256, loki, 0.02 s)
    converted  rock_nohq.png -> rock_nohq.paa (256x256, loki, 0.02 s)
    done: 3 converted, 0 skipped, 1 flagged, 0 need Arma 3 Tools, 1 errors (0.0 s)

If a file ever needs ImageToPAA and it is not found, its line reads `needs-tool NAME: needs Arma 3 Tools for _SUFFIX` and the exit code is 1.

## Exit codes

The same list is at the end of `Loki-cli.exe --help`.

- 0  all files converted or up to date
- 1  any file flagged, needing Arma 3 Tools, or failed
- 2  usage error (bad option, no picture file matched)

## PowerShell notes

In PowerShell 5.1, `> file` writes UTF-16; use `| Out-File -Encoding utf8 file` for UTF-8. Run the exe as `.\Loki-cli.exe` from its folder, or add the folder to PATH. Keep quotes around glob patterns so Loki expands them itself.
