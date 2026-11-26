# loki-paa

PAA texture encoder (DXT1/DXT5 and the uncompressed formats, with mips) used by Loki. MIT + Commons Clause (source-available; see the repo LICENSE), Copyright (c) 2026 Astrid Winters.

One crate, three faces:
- Rust library `loki_paa`: `encode_file`, `supported`, `classify`, the rule table (`rules`), and the pieces they use. Single-threaded; no thread pool.
- Python extension `_paa` (feature `python`): `supported(name)` and `encode(png, paa, max_size)`. Build and copy with `python build/build_native.py`.
- CLI `paa-proto`: `encode`, `decode`, `info`, `rules`; kept to byte-compare against other encoders.

Terms: a mip is one level of the half-size chain; AVGC/MAXC/FLAG/SWIZ are tags in the PAA header; `_co`/`_ca`/`_nohq` are file name suffixes that pick the format, swizzle and mip filter (TexConvert naming).

## Rule table
`src/rules.rs` has one entry per TextureHints class of Bohemia's `TexConvert.cfg` (42 classes, version 2.9.2014, convertVersion 6): suffix, format, channel swizzles (including forced 0/1 and `1-X`), mip filter, `dynRange`, `autoreduce` and the other keys. It is our own text of those facts; the cfg itself is never copied into the repo or the exe. `paa-proto rules` prints it; a diff script against an installed cfg checks every class and key against an installed cfg and ends with `ALL ACCOUNTED` or `MISSING: n`.

- `Rule::verified` is the only routing gate: `supported(name)` is true for an unsuffixed name and for a suffix whose rule is verified AND implemented (`Rule::routable`). Every suffix is verified, the noise suffixes `_cat`, `_can`, `_non` included (Loki's own seeded noise in `noise.rs`, matched on measured strength, grain and `_cat` coverage, not byte for byte; checked by the noise-aware part of `verify25.py`): nothing routes to ImageToPAA. No per-file refusal is left for alpha: soft alpha is reproduced (AVGC table `avgc.rs`) and the DXT1 1-bit alpha mask is Loki's own cut-out with ImageToPAA's coverage (`cutout.rs`, an approximation).
- `_cat`, `_can` and `_non` carry mip filters that write noise. Bohemia's exact algorithm could not be reverse engineered; `noise.rs` is a close approximation (seeded, deterministic). `Rule::unreplicable` stays as the hook for a rule the encoder cannot take; no rule is refused now.
- Formats: DXT1, DXT5, ARGB1555, ARGB4444 (both LZSS packed, `argb.rs`), AI88 (BT.601 luma, LZSS packed). Observed facts behind each rule are in the doc comments of `rules.rs`, `encode.rs` and `mipfilter.rs`.

Check: `cargo test --locked` and `cargo clippy --locked --lib -- -W missing_docs -D warnings`. `src/golden.rs` pins the encoder output byte for byte: `_co`, `_ca` and unsuffixed hashes must not change; `_nohq` was re-recorded once (its mip filter and swizzle order changed). `src/tests_rules.rs` has the per-suffix structure table read from ImageToPAA output.
