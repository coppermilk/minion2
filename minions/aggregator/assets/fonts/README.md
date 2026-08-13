# Cabinet ("/comod") fonts

Bundled so the render has a real font inside the Docker image (the slim base
has none). Shelf labels (nick over `$amount`) all use one font.

- `LiberationSerif-BoldItalic.ttf` -- Liberation Serif Bold Italic, SIL Open
  Font License 1.1 (Red Hat). A Times New Roman-metric serif; the same font
  the original prototype picked in Colab. It covers both Latin and Cyrillic,
  so Russian nicks render correctly with no fallback needed.

Wired in `aggregator_constants.json` under `comod.render.font` (and
`comod.render.font_cyrillic`, set to the same file). Paths are relative to the
`minions/aggregator` package. Swap the file to change the look; if a
replacement lacks Cyrillic, point `font_cyrillic` at a Cyrillic-capable font.
