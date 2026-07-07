# Bananaland

A personal media pipeline: media items (photos, videos, links) come
in over two transports (Telegram, watched folders), each bot applies
exactly one small transformation, and everything lands in one
directory tree rooted at `DRIVE`.

- Design, requirements, traceability: [BLUEPRINT.md](BLUEPRINT.md)
- Operations, failure modes, recovery: [OPERATIONS.md](OPERATIONS.md)

## Layout

```
minion_core/   kernel (the belt), Settings, prompts, adapters
minions/       one directory per bot; streaming or batch
tests/         requirement-based suite + structural analysis
docker/        one image, N containers; docker-compose.yml at root
deploy/        crontab example, kindle Apps Script (off-kernel)
```

## Quick start

```
cp .env.example .env       # DRIVE (absolute), TG_TOKEN_<BOT>, TG_CHATS
pip install -e '.[dev]'
pytest                     # hermetic: no network, no models
python -m minions.inbox.main
```

Docker (NAS): set `DRIVE_NAS` in `.env`. Windows runs only print and
catch -- double-click `deploy/windows/run.cmd` (it bypasses the
PowerShell execution policy; don't run the `.ps1` directly) or point
Task Scheduler at it. On startup it refreshes the Python requirements
and reinstalls `minion_core` (editable) whenever `pyproject.toml`
changed, so a `git pull` never leaves the bots on a stale env; an
unchanged env is a fast no-op. Each bot runs in exactly one place. The same
`.env` file works on both machines verbatim (paths are validated
against both OS flavors). `model-switch` and `props` need their own
`TG_TOKEN_*` set, or those two containers idle-restart harmlessly.

The image is built on GitHub and published to GHCR on every push to
main (`.github/workflows/image.yml`); the NAS pulls it rather than
compiling torch, so an update is a quick layer download. One
self-healing command does everything -- point DSM Task Scheduler
(root, weekly) at `deploy/nas-update.sh`:

- force-syncs the checkout to `origin/main` even in a non-empty
  folder (`git init` + hard reset, never `git clone`), keeping your
  git-ignored `.env`;
- pulls the fresh image *before* touching the bots, then recreates the
  stack cleanly and prunes old images;
- pulls the local Qwen model itself, so you never run `docker compose
  exec ... ollama pull` by hand.

Make the GHCR package public once for anonymous pulls (or `docker
login`).

## The bots

| Bot | Kind | Behaviour |
|-----|------|-----------|
| inbox | streaming | Telegram file -> `_inbox/` |
| fetch | streaming | link -> video (sink: chat / fan queue) |
| fan-save | streaming | link (TikTok/YouTube/...) -> video parked in `bots/fan-save/done/<MMDD> <title>/` (link spool kept in its `_done/`) |
| frames | streaming | video/link -> every 5th frame into `done/<MMDD> <clip>/` (clip filed in `_done/`), summary to chat (never the frames); `FRAMES_WATCH` adds a folder dock |
| censor-blur | streaming | photo -> people's silhouettes blurred (segmentation) -> chat + `done/<MMDD> <name>/` with the original in `_done/` (`CENSOR_BLUR_WATCH` adds a folder dock) |
| censor-black | streaming | photo -> faces blacked out -> chat + `done/<MMDD> <name>/` with the original in `_done/` (`CENSOR_BLACK_WATCH` adds a folder dock) |
| restore | streaming | photo -> people blurred, then the LLM repaints the scene without them -> chat + a `done/` folder (`RESTORE_WATCH` adds a folder dock) |
| sort | batch | classifies images IN PLACE in `_inbox/` the moment they land (the active backend -> prim name + EXIF fandom + week tag; CLIP decides instantly when it punts); the working week stays in `_inbox/` |
| catch | streaming | new Downloads image -> prim-named copy straight into `pictures/<Fandom>/`; the original never leaves `CATCH_DIR` |
| week-clean | batch | Monday, mechanical: strip the week tag, shelve each classified image into `pictures/<Fandom>/` per its EXIF; unclassified files stay for retry |
| model-switch | streaming | Telegram command bot: `local` / `gemini` / `status` flips the classify+props backend at runtime (no restart) |
| props | streaming | scenario (pasted, or the weekly script) -> recommended props, split into what the `Pr*` library has vs. still needs |
| print | streaming | PDF in `print/` -> spooler -> `print/_done/` (`PRINT_SPOOLER`: lp / SumatraPDF) |
| kindle | outlier | Apps Script, `deploy/apps_script/` |

Model backend: classification and the props bot run behind one adapter
(`minion_core/adapters/backend.py`) over interchangeable models. The
default is a **local Qwen2.5-VL** in an Ollama container ("gem") -- the NAS
classifies with no cloud; the `model-switch` bot flips to **Gemini** and
back at runtime. Background restore stays Gemini-only (image generation).
One-time on the NAS: `docker compose exec ollama ollama pull qwen2.5vl:7b`.

Telegram contract: each bot is its own Telegram identity
(`TG_TOKEN_<BOT>`), and files cross Telegram as documents only, both
directions -- compressed photos/videos are ignored with a logged
reason, results come back via sendDocument (never recompressed).

## CI gates

`ruff check` (select=ALL), `ruff format --check`, `mypy` (strict),
`pytest` (includes the ASCII gate and the import-boundary analysis),
wheel build. All green on main.
