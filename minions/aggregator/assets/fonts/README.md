# Cabinet ("/comod") fonts

Bundled so the render has real fonts inside the Docker image (the slim base
has none). Shelf labels pick a font per line: **Aleo** for Latin nicks and the
`$amount`, and **Roboto Slab** for any line containing Cyrillic (Aleo has no
Cyrillic glyphs, so a Russian nick would otherwise render as empty boxes). Both
are slab-serifs, so a mixed cabinet still reads as one style.

- `Aleo.ttf` -- Aleo (variable), SIL Open Font License 1.1.
  Source: github.com/google/fonts `ofl/aleo/Aleo[wght].ttf`.
- `RobotoSlab.ttf` -- Roboto Slab (variable), Apache License 2.0.
  Source: github.com/google/fonts `apache/robotoslab/RobotoSlab[wght].ttf`.

Wired in `aggregator_constants.json` under `comod.render.font` (Latin) and
`comod.render.font_cyrillic` (fallback). Paths are relative to the
`minions/aggregator` package. Swap either file to change the look; if a font
lacks Cyrillic, keep a Cyrillic-capable one as `font_cyrillic`.
