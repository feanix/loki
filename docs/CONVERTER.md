# The converters

The dropdown at the top of the window picks the converter, and the choice is remembered. Switching it never marks existing PAAs as out of date: a row is "Up to date" or "Needs converting" from the PAA's size and age alone.

- **Both converters** take PNG, TGA and JPG sources through one decoder: a TGA or JPG is decoded to 8-bit RGB or RGBA pixels and then handled exactly as a PNG of those pixels, so the PAA is the same. TGA: 8-bit grey, 16-bit (1555), 24-bit, 32-bit with alpha and colour-mapped, raw or RLE, either origin. JPG: baseline and progressive, grey and colour. CMYK JPGs are refused. A 16-bit TGA with no alpha bits in its header has no transparency.
- **Loki converter** (default): Loki's own encoder, written in Rust and built into `Loki.exe`. A PNG bigger than 4096 on a side (up to 16384, and 8192x8192 pixels in all) is scaled down to 4096 first, shape kept, the way ImageToPAA does; Max size still applies on top.
- **Bohemia ImageToPAA**: every file goes through Arma 3 Tools' `ImageToPAA.exe`.

## Which converter takes a file

With the Loki converter selected, the file name suffix (the part after the last underscore) decides, the same way Bohemia's `TexConvert.cfg` picks a texture type:

- **Loki converter**: a name with no suffix, a suffix `TexConvert.cfg` does not name (it gets the same default rule as no suffix, so `my_rock.png` and `rock_foo.png` are encoded by Loki), or any of `_co _ca _cdt _lco _mco _lca _mask _mc _pr _draftlco _detail _dt _dtsmdi _as _ads _adshq _sm _smdi _no _nohq _novhq _nofhq _nshq _nopx _nof _ns _normalmap _noex _nofex _nsex _raw _1555 _4444 _88 _8888 _gs _dxt1 _dxt5 _sky _cat _can _non`. For example `rock_co.png`, `rock_nohq.png`, `rock_smdi.png`, `rock.png`. The authoritative list is `_paa.supported` in the encoder; it is checked against ImageToPAA's output per suffix.
- **ImageToPAA** (automatic, needs Arma 3 Tools): none. Every texture type is written by the Loki converter, the noise suffixes `_cat`, `_can` and `_non` included.

The Loki converter writes every texture type itself. No suffix goes to ImageToPAA automatically.

## Arma 3 Tools

Arma 3 Tools (free on Steam, in the Tools section of your library) is optional. ImageToPAA is not bundled with Loki. Loki finds it automatically through Steam. If it cannot and something needs it, a "Browse..." button appears so you can point it at `ImageToPAA.exe` yourself (on the command line: `--imagetopaa PATH`). Without Arma 3 Tools nothing warns you until a row needs it (or Bohemia is selected); that row says "needs Arma 3 Tools for _cat" and is not converted.

## Known limits

Three kinds of output are not byte for byte what ImageToPAA writes. All were measured against it, and Loki does not hand any of them to ImageToPAA.

- **Soft alpha** (smooth transparency, for example glass or foliage in `_ca`, or any unsuffixed file with soft alpha): the colour that ImageToPAA stores as the texture's average (the AVGC tag, which the game uses for far away views) follows an alpha-dependent rule it does not document. Loki reproduces it from a table measured on ImageToPAA's own output: within 1 level of 255 on every one of 57 files tried (49 exact), real textures included. The mip chain is identical to ImageToPAA's; the DXT5 alpha blocks of detailed soft art can be up to about 2 dB less exact than ImageToPAA's (both sit near 40 dB from the source, which is not visible).
- **DXT1 cut-out alpha** (a `DXT1` type with a 1-bit transparency mask, for example hair cards in `_co`): ImageToPAA dithers the mask. Its rule could not be reverse engineered: a flat alpha is not dithered (opaque from alpha 4 up), a ramp gives a regular 8 texel pattern, and the same 4x4 block is masked in up to three ways depending on its neighbours. Loki cuts its own mask instead: texels of a block of one alpha are opaque from alpha 4, every other texel by one threshold per mip, chosen so that the share of opaque texels equals the mean alpha of that mip (a plain cut at 128 on art whose alpha is mostly 0 or 255). Checked against ImageToPAA on a real hair card (1024x1024): 99.65 percent of the texels of mip 0 have the same bit, and the opaque share stays within 0.7 percentage points of ImageToPAA's on every mip down to 16x16 (4 points at worst on a large binary cut-out). Checked in game with Loki beside ImageToPAA on panels side by side: a real hair card and a large real cut-out looked the same, and a generated hair card looked slightly better with Loki.
- **Noise suffixes** (`_cat` alpha test, `_can`, `_non`): Bohemia's exact noise algorithm for these could not be reverse engineered, so Loki uses its own close approximation. It is seeded, so the same picture always gives the same file, and it is matched on what ImageToPAA's output measures as: the strength of the noise, its grain, its spectrum (the share of the noise power in each frequency band, so it has the same medium grain, without cloudy clumps or single-texel speckle) and, for `_cat` and the alpha of `_non`, the share of texels that pass an alpha test at 128, on every mip. The bytes therefore differ from ImageToPAA's, but the texture behaves the same in game. Measured against ImageToPAA on 157 generated cases (foliage, fences of 25, 50 and 75 percent, gradients, noise and normal maps; 256 to 4096 on a side, square and not): noise strength within 15 percent of ImageToPAA's on every mip of 16x16 and up (13.5 percent at worst), grain within one texel and `_cat` coverage within 3 percentage points on every mip of 64x64 and up (1.9 points at worst). The share of noise power in each of seven radial bands (0 to 1/64, 1/32, 1/16, 1/8, 1/4, 1/2 and 0.71 cycles per texel) is within 0.05 of ImageToPAA's on 1024 and 2048 pictures, mips 0 to 2 (0.017 at worst). `_cat` makes about half of the texels solid on every mip whatever the source, as ImageToPAA's does: a fence that is 78 percent solid in the picture is about 51 percent solid in the file, and a flat opaque `_cat` picture is about half transparent. `_non` keeps its normals like `_nohq` and, like ImageToPAA, replaces its alpha with the same noise (the picture's alpha is ignored). On the smallest mips (32x32 and below) ImageToPAA's own coverage varies by up to 5 points with the picture and Loki follows it less closely. Details and the numbers: [PAA-FORMAT.md](PAA-FORMAT.md), Noise filters.
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
