# Cabinet ("/comod") assets

Drop the **empty cabinet photo** here as `cabinet.jpg` -- this is the base
image the `/comod` command draws shelf labels (nick over amount) onto.

- Path expected by the config: `minions/aggregator/assets/cabinet.jpg`
  (set in `aggregator_constants.json` under `comod.render.template`; a
  relative path is resolved against the `minions/aggregator` package).
- The default shelf coordinates (`comod.render.slots`) match a **1080-wide
  portrait** cabinet with **10 slots**: the top full-width shelf, three rows
  of paired cubbies, then three wide lower shelves. If you use a photo with a
  different size or layout, update `comod.render.slots` (`[x, y, w, h]` boxes)
  to match.
- Until this file exists, `/comod` falls back to posting a plain-text roster
  instead of the rendered image -- the feature still works, just without the
  picture.

The photo is a binary asset and is intentionally not committed by the tooling;
add your own here.
