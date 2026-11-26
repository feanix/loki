# PAA format notes

MIT License. Copyright (c) 2026 Astrid Winters. Part of Loki; see `LICENSE` in the repository root.

Notes on the Arma 3 PAA texture format, collected while writing Loki's own encoder (`native/paa`). They are research notes from before the encoder existed: the encoder now covers more than this document (more suffixes, ARGB layouts, 1-bit alpha), and an "untested" entry here means "not covered by these notes", not "unknown to the encoder". Where this file and the encoder disagree, the encoder and its tests are right. Evidence tags:
- **[obs: file]** observed in ImageToPAA output of the named sample file (trusted most).
- **[armake]** https://raw.githubusercontent.com/KoffeinFlummi/armake/master/src/img2paa.c (read as a summary, not checked byte by byte).
- **[cfg]** TexConvert.cfg defaults as mirrored in https://pkg.go.dev/github.com/woozymasta/paa/texconfig (default_values.go, MIT) and a 2007 copy at https://www.pmctactical.org/forum/viewtopic.php?t=21178.
- **[armaio]** https://armaio.readthedocs.io/stable/paa/format/
- **[wiki]** BI community wiki PAA page. NOT READ DIRECTLY (HTTP 403 for community.bistudio.com and community.bohemia.net). Only search-result snippets: https://community.bistudio.com/wiki/PAA .
- **[unsourced]** inference or general knowledge, verify.

Where docs and observation disagree, observation wins.

## 1. File layout

All little endian. [obs: all 12 fixtures; armake agrees]

```
u16  type            0xFF01 DXT1 (bytes 01 ff), 0xFF05 DXT5 (05 ff)
TAGG*                AVGC, MAXC, [FLAG | SWIZ], OFFS   (this order)
u16  palette_count   0 always
mip*                 u16 width(+flag), u16 height, u24 data_len, data
terminator           6 zero bytes after last mip
```
- Other type magics (not needed): ARGB1555 0x1555, ARGB4444 0x4444, ARGB8888 0x8888, GRAY 0x8080, DXT2-4 0xFF02-04. [armaio] "DXT1 = 65281 (0xFF01) ... DXT5 = 65285 (0xFF05)". armake img2paa type 0 = auto: DXT5 if alpha else DXT1. [armake]
- Terminator: armake writes 4 zero bytes then 2 more zero bytes [armake]; observed tail `00 00 00 00 00 00`, total size = last mip end + 6 [obs: every file]. The loop-ending entry is w=0,h=0 (4 bytes), plus 2 further zero bytes (meaning unknown, write them).
- Dimensions must be multiples of 4 [armake: `width % 4 != 0 || height % 4 != 0` rejected]. Power-of-two assumed by wiki snippet ("each one conforms to 2^n dimensions" [wiki, snippet]); non-pow2 untested.

## 2. TAGGs

Each: 4 bytes `GGAT` ("TAGG" reversed), 4 name bytes reversed, u32 data length, data. [obs: offset 2 `47 47 41 54`; armake writes `"GGATCGVA"`, `"GGATCXAM"`, `"GGATSFFO"`]

| tag | on disk | len | content | present |
|---|---|---|---|---|
| AVGC | `CGVA` | 4 | average colour, bytes B,G,R,A. Alpha = mean alpha (203/202 in RGBA test), 255 if opaque [obs: rgba256_ca.paa, rgb256_co.paa] | always, first |
| MAXC | `CXAM` | 4 | `ff ff ff ff` in all 12 fixtures [obs]; armaio: "usually (255, 255, 255, 255)" [armaio] | always, second |
| FLAG | `GALF` | 4 | u32 = 1 when input has real alpha (DXT5 from RGBA) [obs: rgba256*.paa]. Absent for RGB DXT1 and _nohq DXT5. armaio enum: NONE 0, INTERPOLATED 1, BINARY 2 [armaio]; 2 never observed | alpha inputs only |
| SWIZ | `ZIWS` | 4 | bytes `05 04 02 03` on _nohq [obs: normal256_nohq.paa]. Matches wiki snippet "fixed data value 0x05040203" [wiki, snippet]. Code meaning not stated by source | _nohq only (observed) |
| OFFS | `SFFO` | 64 | 16 x u32 absolute file offsets of mip entries, zero padded [obs; armake: length 0x40] | always, last |

- Order observed: AVGC, MAXC, [FLAG|SWIZ], OFFS [obs]. Wiki snippet: AVG first, MAX second, SWIZ or PROC third, OFFS last; FLAG/PROC/SWIZ optional, each at most once [wiki, snippet]. FLAG and SWIZ never co-occurred.
- Required: AVGC, MAXC, OFFS are what armake writes [armake] and ImageToPAA always writes [obs]. Arma tolerance of missing ones: untested ([unsourced]). Always write all three.
- armake does NOT write FLAG or SWIZ [armake, as summarized]; wiki says FLAG "must be present when binarizing models" [wiki, snippet]; armaio: without it model faces are not marked alpha at P3D binarization. Write FLAG for alpha textures as ImageToPAA does.
- OFFS[0] = offset of first mip = (offset of palette_count) + 2: 112 without FLAG/SWIZ, 128 with [obs]. Sizes: AVGC/MAXC/FLAG/SWIZ tags 16 bytes each, OFFS 72.
- Writing OFFS: armake writes zeros then back-patches [armake]. In Rust, compute mip sizes first and emit once.

## 3. Palette

u16 count right after OFFS. Always 0 [obs: all]; armake: "a 2-byte zero value, meaning no palette" [armake]. Nonzero would be count x 3 bytes (never seen). Do not implement.

## 4. Mip entries

```
u16 width   bit15 (0x8000) = data compressed; real width = w & 0x7FFF
u16 height  (never flagged in our files)
u24 length  stored byte count (compressed length if flagged)
data
```
- Entry offset + 7 + length = next entry [obs]. armake: `fwrite(&datalen, 3, 1, ...)`; `width += 32768` before write when compressed [armake].
- Raw length = DXT size: DXT1 w*h/2, DXT5 w*h (8 / 16 B per 4x4 block) [obs: 128x128 DXT1 8192, DXT5 16384; armake `datalen = width*height; if DXT1 datalen /= 2`].
- u24 max 16,777,215; 4096x4096 DXT5 raw is 16,777,216 (overflow) so it must be compressed or rejected [unsourced]. Wiki TexConvert.cfg snippet gives default max size 4096.
- Order: largest first, halving each step [obs, armake].

### Mip count and minimum size
- Halve w and h each level; stop when next level has w < 4 or h < 4 [armake: `if (width < 4 || height < 4) break;`, up to 15 levels]. 1x1 and 2x2 NOT written [obs].
- Square 256 -> 7 mips (256..4); 2048 -> 10 mips [obs]. 512x128 -> 512x128, 256x64, 128x32, 64x16, 32x8, 16x4 = 6 mips; short side stops at 4 [obs: wide512x128_co.paa].
- Mip filter not in file; TexConvert.cfg has per-class `mipmapFilter` (FadeOut, NormalizeNormalMapAlpha, AlphaNoise ...) [cfg]. Box filter is our assumption [unsourced].

## 5. Extra compression layer

- Algorithm: LZO1X. [armake: `lzo1x_1_compress`; https://github.com/WoozyMasta/paa: "LZO and LZSS decompression for mip data"]. Head bytes agree: an LZO1X stream starting with byte b > 17 begins with a literal run of b-17 bytes ([unsourced], general LZO1X). rgb256_co.paa mip0 starts `19 e0 ff 21 08 40 00 00`: 0x19-17 = 8 literals starting `e0 ff 21 08 40 00 00`, a DXT1 block with c0 = 0xFFE0. DXT5 heads `14 80 80 00 ...` = 3 literals `80 80 00` (alpha endpoints 0x80,0x80). LZO1X likely but NOT confirmed by decompressing (no LZO library was at hand). To confirm: decompress a flagged mip with any LZO1X decoder, expect w*h/2 or w*h bytes.
- -1 vs -999 variant used by ImageToPAA: unknown. Any valid LZO1X stream decodes identically, so any LZO1X compressor may do. Arma accepting any valid stream: assumed ([unsourced]).
- When (ImageToPAA): flagged iff width >= 256 [obs]. 256x64 DXT1 (raw 8192 B) is flagged while 128x128 DXT1 (raw 8192 B) is not (wide512x128_co.paa), so it is a dimension rule, not a byte threshold. w vs max(w,h) not separable (no tall images tested).
- armake: `compressed = args.compress && datalen > LZO1X_MEM_COMPRESS`, only with `-z` [armake]. This DISAGREES with observation (32 KiB DXT1 mips compressed). Trust observation.
- Compressed >= raw: untested; safe rule store raw unflagged. armake flags whenever it chose compression [armake].
- Flag lives in the width field only; reader: `(w & 0x8000) != 0` [obs, armake].
- LZSS page exists (https://community.bistudio.com/wiki/Compressed_LZSS_File_Format), not read; not seen in DXT PAA. Do not implement.
- Compression is optional for validity: small mips are raw with flag clear [obs]; armake without -z writes all raw. v1 may skip LZO; add after in-game round trip.

## 6. DXT block layout

Source: https://en.wikipedia.org/wiki/S3_Texture_Compression
- DXT1, 8 bytes per 4x4 block: "two 16-bit RGB 5:6:5 color values c0 and c1" then 16 x 2-bit indices (32 bits). c0 > c1 (unsigned u16): c2 = 2/3 c0 + 1/3 c1, c3 = 1/3 c0 + 2/3 c1. c0 <= c1: c2 midpoint, c3 transparent black.
- DXT5, 16 bytes: first 8 = alpha block (alpha0 u8, alpha1 u8, 16 x 3-bit indices = 48 bits); alpha0 > alpha1: 6 interpolated values (8-value mode), else 4 interpolated plus 0 and 255. Last 8 = DXT1-style colour block.
- Block order and index bit order: the Wikipedia page does not state them. Standard D3D BC1/BC3: blocks row-major, indices LSB-first, row 0 first ([unsourced]). Verify via ImageToPAA readback (section 8).
- Observed DXT1 first block of large mips: `e0 ff 21 08 ...` [obs] consistent with standard layout.
- ImageToPAA never used DXT1 for RGBA in tests (_ca and no-suffix RGBA both DXT5); punch-through DXT1 not emitted.
- armake uses stb_dxt (`stb_compress_dxt_block`) [armake]; stb_dxt licence not verified here. Write our own range-fit/cluster-fit.

## 7. Format choice by suffix

TexConvert.cfg drives it ("configuration file for TexView/ImageToPAA ... parameters used for packing PAA files"; `name` is a wildcard on the input filename [search snippet of https://community.bistudio.com/wiki/TexConvert.cfg]; page 403). Defaults [cfg]:

| suffix | format | swizzle / filter [cfg] | observed |
|---|---|---|---|
| `_co` | DXT1, dynRange 1 | | DXT1 [obs: rgb256_co] |
| `_ca` | DXT5, dynRange 1 | | DXT5 + FLAG=1 on RGBA [obs: rgba256_ca] |
| `_cat` | DXT5 | AlphaNoise: alpha of every mip, mip 0 included, rewritten by noise | DXT5, tags as `_ca` [obs, see Noise filters] |
| `_can` | DXT5 | AddAlphaNoise: alpha 0 stays 0, else 64 + alpha/2 + noise | DXT5, tags as `_ca` [obs] |
| `_raw`, `_1555` | ARGB1555 | | untested |
| `_4444` | ARGB4444 | | untested |
| `_sky` | DXT5 | R,1-A,B,1-G | untested |
| `_detail`,`_dt` | DXT1 | A,A,A,1; FadeOut | untested |
| `_cdt` | DXT1 | FadeOut | untested |
| `_lco`,`_mco`,`_mask` | DXT1 | autoReduce | untested |
| `_lca` | DXT5 | | untested |
| `_pr` | DXT5 | autoReduce | untested |
| `_mc` | DXT5 | | untested |
| `_as` | DXT1 | 1,G,1,1 | untested |
| `_ads` | DXT1 | 1,G,B,1 | untested |
| `_adshq` | DXT5 | 0,B,0,G | untested |
| `_sm` | DXT1 | R,G,B,1 | untested |
| `_smdi` | DXT1 | 1,G,B,1 | untested |
| `_dtsmdi` | DXT1 | R,G,B,1 | untested |
| `_no` | DXT1 | A=1; normal filter | untested |
| `_nohq` | DXT5 | cfg 1-A,G,B,1-R; NormalizeNormalMapAlpha | DXT5, SWIZ `05 04 02 03`, no FLAG [obs: normal256_nohq] |
| `_novhq` | DXT5 | 1,G,1,1-R | untested |
| `_nofhq`,`_nshq` | DXT5 | as nohq | untested |
| `_non` | DXT5 | NormalizeNormalMapNoise: normals as `_nohq` (alpha weighted); alpha is pure noise | DXT5, no SWIZ [obs] |
| `_nopx` | DXT5 | | untested |
| `_normalmap`,`_nof`,`_ns` | unset | normal filters | untested |
| `_noex`,`_nofex`,`_nsex` | ARGB4444 | uncompressed | untested |
| `_gs` | AI88 | | untested |
| `_dxt1`,`_dxt5` | explicit | | untested |

- The 2007 forum copy and the Go copy differ in places (swizzle order of `_nohq`, `_as`, `_smdi`), so the swizzle column is low confidence. Only _co, _ca, _nohq, no-suffix observed. SWIZ payload per suffix unknown beyond `_nohq` = `05 04 02 03` [obs]. _nohq AVGC equals the average of the input RGB, alpha 255 [obs], so AVGC is computed on source data (or ambiguity if swizzle only touches alpha).
- **No suffix**: auto. RGB -> DXT1; RGBA -> DXT5 + FLAG=1 [obs: rgb256.paa, rgba256.paa; armake auto: "DXT5 if the image has alpha, otherwise DXT1"]. Whether "has alpha" means a PNG alpha channel or any pixel alpha < 255: untested.
- No-suffix vs suffixed differ only in mip bytes and AVGC alpha by 1 (rgb256 vs rgb256_co mip0 22118 vs 22152; rgba AVGC alpha 202 vs 203); tags identical [obs]. Likely `dynRange` (1 for _co/_ca) [cfg, hypothesis, unconfirmed].

### Noise filters (`_cat`, `_can`, `_non`)

Bohemia's exact noise algorithm for these three suffixes could not be reverse engineered: its generator, its seed and its mapping cannot be recovered from the output. Loki uses its own close approximation (`native/paa/src/noise.rs`), matched on measured noise strength, grain and, for `_cat` and the alpha of `_non`, alpha coverage per mip. The bytes therefore differ from ImageToPAA's; the texture behaves the same in game. What ImageToPAA's output shows [obs, decoded values, noise = output minus the plain filter through the same codec]:

- Every mip, mip 0 included, is rewritten from the plain box mean `b` of the source alpha at that level (the chain is not fed back). The noise field depends on the level size only: the same level size of two inputs, or of two runs, has the same field.
- `_can`: alpha 0 stays exactly 0; any other texel is `64 + b/2 + noise`. The noise is bell shaped (excess kurtosis about -0.4), standard deviation 16.1, the same on every mip (it is not averaged down), white (per texel) below 512 texels and band limited above (see Grain and spectrum). The 4x4 level has no noise.
- `_cat`: the alpha of each mip is rank-equalised. Whatever the source (flat 0, flat 255, a fence that is 78 percent solid, a 25 percent soft disc), the output is spread evenly over 0..255 and 50 to 54 percent of the texels have alpha at or above 128 on every mip of 16x16 and up. Texels keep their source order, blurred by noise of standard deviation about 32 on the 0..255 scale: a hard cut-out keeps its shape (source 0 gives the lower half, source 255 the upper half), a gradient keeps its slope. Flat alpha gives uniform 0..255 (mean 129.7, standard deviation 73.5); a flat 4x4 level is all 255.
- `_non`: the colour is a normal map filtered like `_nohq` (colour weighted by the source alpha, each level renormalised); the alpha is `_cat`'s noise on a flat input: uniform 0..255 and uncorrelated with the source alpha (correlation 0.00 on a cut-out, a gradient and a foliage mask).
- Grain and spectrum: the noise of a level depends on its size. Below 512 texels it is white (per texel). From 512 up it is band limited, with a radial power spectrum that moves down in frequency with the size: on the 1024 level almost no power above 0.5 cycles per texel (0.03) and almost none below 1/32 (0.002), on 2048 almost none above 0.25 (0.05) and 0.03 below 1/32 (lag 1 / lag 2 correlation 0.3 / -0.1 at 1024, 0.76 / 0.31 at 2048, 0.94 / 0.78 at 4096). Share of the noise power per radial band (0-1/64, 1/64-1/32, 1/32-1/16, 1/16-1/8, 1/8-1/4, 1/4-1/2, 1/2-0.71 cycles per texel), `_cat` flat input: 1024 level 0.000 / 0.000 / 0.002 / 0.028 / 0.274 / 0.663 / 0.033; 2048 level 0.000 / 0.002 / 0.028 / 0.275 / 0.641 / 0.051 / 0.003; 512 level 0.000 / 0.001 / 0.003 / 0.012 / 0.083 / 0.612 / 0.289. The spectrum is isotropic and the same for `_cat`, `_non` and (within 0.02) `_can`. `research/paa-noise/SPECTRUM41.md`.
- AVGC, MAXC, FLAG and SWIZ are those of the plain class (`_ca`; `_non` has no swizzle): AVGC comes from the source colour and the plain mean source alpha, not from the noise.

Measured agreement of Loki's approximation (157 generated cases, 256 to 4096 on a side, square and 512x128 / 128x512; `research/paa-noise/measure_ext.py`): noise standard deviation within 15 percent of ImageToPAA's on every mip of 16x16 and up (13.5 percent at worst, `_can`), grain within one texel and `_cat` coverage within 3 percentage points on every mip of 64x64 and up (1.9 points at worst). On levels of 32x32 and below ImageToPAA's own `_cat` coverage varies by up to 5 points with the input (0.48 to 0.57) and Loki follows it less closely; the 8x8 and 4x4 levels of ImageToPAA are not modelled.

Spectrum (`research/paa-noise/spectrum41.py`): Loki builds the noise of levels of 512 to 4095 texels in the Fourier domain from ImageToPAA's measured radial profile per level size (`native/paa/src/noise_profile.rs`, fitted by `research/paa-noise/fit_profile41.py`), so the share of noise power in each of the seven bands above is within 0.05 of ImageToPAA's (0.017 at worst; 0.006 on the 1024 and 2048 levels) for `_cat`, `_can` and `_non`, on 1024 and 2048 pictures, mips 0 to 2. Levels of 4096 and up keep the older smoothed white noise (lag correlations above); their spectrum was not measured.

## 8. Validation

`ImageToPAA.exe x.paa x.png` reads PAA back to PNG (exit 0, 256x256 RGBA from rgba256_ca.paa) [obs]. Use it as the decoding reference: encode ours, decode with ImageToPAA, compare to source with tolerance; byte-compare structure (tags, OFFS, lengths, flags) against ImageToPAA output of same PNG. RGB-only readback mode and mip selection untested.

## 9. Open questions and risks

1. LZO variant only inferred; confirm by decompression, check end marker (`11 00 00`, [unsourced]), and that Arma accepts another compressor's stream.
2. Flag rule width >= 256 vs max(w,h) vs height: test 128x512, 256x128.
3. Game acceptance never tested: no TexView 2 / game load of our output. Required tags, FLAG necessity, tail bytes validated only by mimicry.
4. Mip filter and dynRange unknown; normal maps (`_nohq`, `_no`, `_ns`) need exact swizzle and renormalization or lighting is wrong. High risk.
5. SWIZ semantics: one sample. Other swizzled suffixes may emit other SWIZ or none; dump ImageToPAA output per suffix before supporting.
6. FLAG=2 and 1-bit alpha untested.
7. Non-pow2 / non-multiple-of-4 input undefined; reject (armake does).
8. u24 overflow for 4096 DXT5 uncompressed.
9. Colour space (sRGB vs linear) in AVGC and DXT endpoints unknown; MAXC with dark images untested.
10. Wiki PAA and TexConvert.cfg pages not read (403); re-read via browser or archive.org for PROC tag, palette, 16-bit types.
11. 2 trailing zero bytes purpose unknown.
12. Licences: section 10.

## 10. Prior art and licences

Rule for Loki: learn the format from these, write original code; no line-by-line translation of any code, especially GPL. Read for format facts only.

| project | what | licence | use |
|---|---|---|---|
| armake, https://github.com/KoffeinFlummi/armake | C, `img2paa.c`: PAA via stb_dxt + LZO | GPL v2 or later (Copyright 2016 Felix Wiegand) | learn only; no translation, no linking |
| armake2, https://github.com/KoffeinFlummi/armake2 | Rust successor; README says "PAA commands not implemented" but usage lists img2paa | GPL-2.0 | learn only |
| ArmaIO, https://github.com/MrClock8163/ArmaIO ; https://armaio.readthedocs.io/stable/paa/format/ | Blender addon, Python PAA reader | licence not checked | docs for enums |
| WoozyMasta/paa, https://github.com/WoozyMasta/paa | Go PAA read/write, TexConvert.cfg parser and defaults | MIT | readable reference; still write our own |
| lzokay-rs, https://github.com/encounter/lzokay-rs | Rust wrapper of LZ👌 minimal LZO | MIT per search snippet | candidate dependency |
| lzokay-native-rs, https://github.com/arma-tools/lzokay-native-rs | pure Rust lzokay port | not read | candidate |
| stb_dxt (nothings/stb) | DXT block compressor | not verified (PD/MIT expected) | learn algorithm only |
| Mikero tools / PboProject | not researched, closed source | n/a | skip |
| BI Arma 3 Tools (ImageToPAA, TexView 2) | reference encoder, decoding reference | proprietary, external tool only | reference |

Not read: BI community wiki pages (PAA, TexConvert.cfg, Compressed LZO/LZSS File Format, TexView 2 Manual): 403. PboProject docs: not searched.

## 11. Alpha: the AVGC of soft alpha and the DXT1 1-bit mask

Measured on ImageToPAA 1.0.0.3 (cfg convertVersion 6). Updates section 7.9 (colour space in AVGC): in the classes with `dynRange` 1 or unset the average is computed in linear light with alpha weights, from a per-texel colour that is not the colour byte.

**AVGC with soft alpha (reproduced).** For every texel with colour byte `c` and alpha `a`, ImageToPAA first forms `F(c, a)`: the premultiplied byte `P = floor(c * a / 255)` turned back into linear light and divided by `a / 255`, with a floor near black that falls away as `P` grows (flat grey 200 at alpha 128 gives 138, flat black at alpha 255 gives 1, black at alpha 1 gives 127). AVGC colour is the mean of `F` over the texels in linear light, weighted by alpha, converted back to sRGB; AVGC alpha is the plain mean. `F` depends only on `(a, P)` and is not a closed formula that was found: Loki stores the measured table (`native/paa/src/avgc_soft.bin`, 32,896 bytes, from 65,536 flat-image runs). It is the same in `_ca`, `_co`, the unsuffixed rule, `_8888` and `_gs` and at every size from 4x4. Checked on every flat colour, on mixes of two texel kinds, on generated art and on 33 real textures (57 files in all): every channel within 1 level.

**DXT1 1-bit alpha (approximation).** What was seen: a flat alpha of 4 or more is opaque, 0 to 3 is clear; on a ramp the opaque texels of a block are always the highest-alpha ones, their number follows the block's alpha sum, and a 1-pixel ramp gives a regular 8 texel pattern with thresholds near `8 + 31 j`; the same block among different neighbours was masked in up to three ways, so the mask is not a function of the block; the share of opaque texels of a mip stays near its mean alpha (a real hair card: 15.48 percent opaque against a mean alpha of 15.54 percent, on every mip). What could not be reverse engineered: the exact rule (which texel flips where). What Loki does instead: `native/paa/src/cutout.rs`, a deterministic cut-out: blocks of one alpha opaque from 4, every other texel by one threshold per mip that makes the opaque share equal the mean alpha (closest to 128 when several thresholds give the same count). How it was checked: against ImageToPAA per mip on a real hair card and generated cut-outs (opaque share and equal bits), with the alpha of such cases judged by the opaque share and by a relative check against the source, and in game with the two files side by side: the real hair card (1024) and a large real cut-out (2048) looked the same, and a generated hair card looked slightly better with Loki.
