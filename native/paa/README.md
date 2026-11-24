# loki-paa

PAA texture encoder (DXT1/DXT5 and the uncompressed formats, with mips) used by Loki. MIT + Commons Clause (source-available; see the repo LICENSE), Copyright (c) 2026 Astrid Winters.

One crate, three faces:
- Rust library `loki_paa`: `encode_file`, `supported`, `classify`, the rule table (`rules`), and the pieces they use. Single-threaded; no thread pool.
- Python extension `_paa` (feature `python`): `supported(name)` and `encode(png, paa, max_size)`. Build and copy with `python build/build_native.py`.
- CLI `paa-proto`: `encode`, `decode`, `info`, `rules`; kept to byte-compare against other encoders.

Terms: a mip is one level of the half-size chain; AVGC/MAXC/FLAG/SWIZ are tags in the PAA header; `_co`/`_ca`/`_nohq` are file name suffixes that pick the format, swizzle and mip filter (TexConvert naming).

## Rule table (task #025)
`src/rules.rs` has one entry per TextureHints class of Bohemia's `TexConvert.cfg` (42 classes, version 2.9.2014, convertVersion 6): suffix, format, channel swizzles (including forced 0/1 and `1-X`), mip filter, `dynRange`, `autoreduce` and the other keys. It is our own text of those facts; the cfg itself is never copied into the repo or the exe. `paa-proto rules` prints it; `research/paa-encoder/tools/diff_texconvert.py <cfg> --exe paa-proto` checks every class and key against an installed cfg and ends with `ALL ACCOUNTED` or `MISSING: n`.

- `Rule::verified` is the only routing gate: `supported(name)` is true for an unsuffixed name and for a suffix whose rule is verified AND implemented (`Rule::routable`). Every suffix is verified (research/paa-encoder/VERIFY25.md) except `_sky`, which fails the mip 2 check, and the noise suffixes `_cat`, `_can`, `_non`, which are not replicated: those route to ImageToPAA. `encode_file` also refuses, per file, an image whose ImageToPAA output is not reproduced (`encode::reproducible`: DXT1 1-bit alpha, and soft alpha where AVGC is alpha weighted); the error says "needs ImageToPAA".
- `_cat`, `_can` and `_non` carry mip filters that write noise from ImageToPAA's own generator. They are not replicated: `encode` refuses them (`Rule::unreplicable`) and they always route to ImageToPAA.
- Formats: DXT1, DXT5, ARGB1555, ARGB4444 (both LZSS packed, `argb.rs`), AI88 (BT.601 luma, LZSS packed). Observed facts behind each rule are in the doc comments of `rules.rs`, `encode.rs` and `mipfilter.rs`.

Check: `cargo test --locked` and `cargo clippy --locked --lib -- -W missing_docs -D warnings`. `src/golden.rs` pins the encoder output byte for byte: `_co`, `_ca` and unsuffixed hashes must not change; `_nohq` was re-recorded in #025 (its mip filter and swizzle order changed). `src/tests_rules.rs` has the per-suffix structure table read from ImageToPAA output.
