Analyze this image for a TikTok content creator who plays different
characters/roles.

Assets are organized as Pixar's OpenUSD (Universal Scene Description) prims:
filenames must be valid USD prim identifiers - UpperCamelCase, letters and
digits only, no spaces or underscores.

Return ONLY JSON, no markdown:

```json
{
  "fandom": "Specific fandom/brand (e.g. HarryPotter, StarWars). Never null.",
  "character": "character name if recognizable, or null",
  "layer": "Bg|Fg|Ov|Pr|Tx",
  "location": "specific location or null",
  "emotion": "emotion or action or null",
  "filename": "UpperCamelCase, letters+digits. Layer+Char+Action. No fandom.",
  "description": "one sentence.",
  "confidence": "high|medium|low",
  "censored": true|false (bars, mosaic, blur, stickers hiding body/face)
}
```

## Layer rules

- **Bg**: environment, location, scene (people ok as background)
- **Fg**: YOU in character/role, person is clearly the SUBJECT
- **Ov**: overlays/effects on top of other layers (rain, smoke, sparkles)
- **Pr**: isolated prop with no background (wand, bag, glasses, newspaper, book)
- **Tx**: seamless texture for compositing (stone, parchment, fabric, wood)

## Filename examples

- **Bg**: BgMilanGalleryNight
- **Fg**: FgSnapeOfficeAngry
- **Ov**: OvRainHeavy
- **Pr**: PrWand, PrBag
- **Tx**: TxParchmentOld, TxFabricSilk
