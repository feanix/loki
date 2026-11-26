# The converters

The dropdown at the top of the window picks the converter, and the choice is remembered. Switching it never marks existing PAAs as out of date: a row is "Up to date" or "Needs converting" from the PAA's size and age alone.

- **Both converters** take PNG, TGA and JPG sources through one decoder: a TGA or JPG is decoded to 8-bit RGB or RGBA pixels and then handled exactly as a PNG of those pixels, so the PAA is the same. TGA: 8-bit grey, 16-bit (1555), 24-bit, 32-bit with alpha and colour-mapped, raw or RLE, either origin. JPG: baseline and progressive, grey and colour. CMYK JPGs are refused. A 16-bit TGA with no alpha bits in its header has no transparency.
- **Loki converter** (default): Loki's own encoder, written in Rust and built into `Loki.exe`. A PNG bigger than 4096 on a side (up to 16384, and 8192x8192 pixels in all) is scaled down to 4096 first, shape kept, the way ImageToPAA does; Max size still applies on top.
- **Bohemia ImageToPAA**: every file goes through Arma 3 Tools' `ImageToPAA.exe`.

## Which converter takes a file

With the Loki converter selected, the file name suffix (the part after the last underscore) decides, the same way Bohemia's `TexConvert.cfg` picks a texture type:

- **Loki converter**: a name with no suffix, a suffix `TexConvert.cfg` does not name (it gets the same default rule as no suffix, so `my_rock.png` and `rock_foo.png` are encoded by Loki), or any of `_co _ca _cdt _lco _mco _lca _mask _mc _pr _draftlco _detail _dt _dtsmdi _as _ads _adshq _sm _smdi _no _nohq _novhq _nofhq _nshq _nopx _nof _ns _normalmap _noex _nofex _nsex _raw _1555 _4444 _88 _8888 _gs _dxt1 _dxt5 _sky`. For example `rock_co.png`, `rock_nohq.png`, `rock_smdi.png`, `rock.png`. The authoritative list is `_paa.supported` in the encoder; it is checked against ImageToPAA's output per suffix.
- **ImageToPAA** (automatic, needs Arma 3 Tools): the noise suffixes `_cat`, `_can` and `_non`. They use Bohemia's own noise generator.

The Loki converter writes every texture type itself except the noise suffixes `_cat`, `_can` and `_non`, which go to ImageToPAA.

## Arma 3 Tools

Arma 3 Tools (free on Steam, in the Tools section of your library) is optional. ImageToPAA is not bundled with Loki. Loki finds it automatically through Steam. If it cannot and something needs it, a "Browse..." button appears so you can point it at `ImageToPAA.exe` yourself (on the command line: `--imagetopaa PATH`). Without Arma 3 Tools nothing warns you until a row needs it (or Bohemia is selected); that row says "needs Arma 3 Tools for _cat" and is not converted.

## Known limits

Two kinds of transparency are not byte for byte what ImageToPAA writes. Both were measured against it, and Loki does not hand either to ImageToPAA.

- **Soft alpha** (smooth transparency, for example glass or foliage in `_ca`, or any unsuffixed file with soft alpha): the colour that ImageToPAA stores as the texture's average (the AVGC tag, which the game uses for far away views) follows an alpha-dependent rule it does not document. Loki reproduces it from a table measured on ImageToPAA's own output: within 1 level of 255 on every one of 57 files tried (49 exact), real textures included. The mip chain is identical to ImageToPAA's; the DXT5 alpha blocks of detailed soft art can be up to about 2 dB less exact than ImageToPAA's (both sit near 40 dB from the source, which is not visible).
- **DXT1 cut-out alpha** (a `DXT1` type with a 1-bit transparency mask, for example hair cards in `_co`): ImageToPAA dithers the mask. Its rule could not be reverse engineered: a flat alpha is not dithered (opaque from alpha 4 up), a ramp gives a regular 8 texel pattern, and the same 4x4 block is masked in up to three ways depending on its neighbours. Loki cuts its own mask instead: texels of a block of one alpha are opaque from alpha 4, every other texel by one threshold per mip, chosen so that the share of opaque texels equals the mean alpha of that mip (a plain cut at 128 on art whose alpha is mostly 0 or 255). Checked against ImageToPAA on a real hair card (1024x1024): 99.65 percent of the texels of mip 0 have the same bit, and the opaque share stays within 0.7 percentage points of ImageToPAA's on every mip down to 16x16 (4 points at worst on a large binary cut-out). Checked in game with Loki beside ImageToPAA on panels side by side: a real hair card and a large real cut-out looked the same, and a generated hair card looked slightly better with Loki.
- **PNGs over 16384 on a side, or over 8192x8192 pixels in all**: too big to load. Scale it to 8192 or less in your image editor; Loki then scales it to 4096 itself. The row says so and nothing is converted until you do.
- **4096x4096 with very noisy detail as DXT5** (`_ca`, `_nohq` and similar; also a bigger PNG that is scaled down to 4096): the data does not fit the PAA format's size field, so the row says "too detailed to fit at 4096 as DXT5: set max size 2048". Set max size to 2048 and it converts.

The format notes behind the encoder are in [PAA-FORMAT.md](PAA-FORMAT.md).

## Speed

Measured with `build/bench_speed.py`, converting one 2048x2048 noise texture through Loki's own code path, median of 5 runs after a warm-up, on an Intel Core Ultra 7 265 (20 logical cores), 63 GB RAM, Windows 11 (10.0.26200):

| file | Loki converter | Bohemia ImageToPAA | faster by |
|---|---|---|---|
| `bench_co.png` (2048x2048) | 0.40 s | 5.98 s | 15x |
| `bench_nohq.png` (2048x2048) | 0.81 s | 1.92 s | 2x |

Other machines differ; run the benchmark yourself (it needs Arma 3 Tools). In practice a saved texture is converted in a fraction of a second to a second.
