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

Docker (NAS): set `DRIVE_NAS` in `.env`, then
`docker compose up -d` -- Telegram bots, sort (watch daemon) and
week-clean (cron in the `batch` container). Windows runs only print
and catch via `deploy/windows/register-tasks.ps1`; each bot runs in
exactly one place.

## The bots

| Bot | Kind | Behaviour |
|-----|------|-----------|
| inbox | streaming | Telegram file -> `_inbox/` |
| fetch | streaming | link -> video (sink: chat / fan queue) |
| fan-save | streaming | link (TikTok/YouTube/...) -> video parked in `bots/fan-save/done/` for later processing |
| frames | streaming | video/link -> every 5th frame, named `<timecode>_<video name>.jpg` -> chat or done dir (`FRAMES_WATCH` adds a folder dock) |
| censor-blur | streaming | photo -> people blurred -> chat or done dir (`CENSOR_BLUR_WATCH` adds a folder dock) |
| censor-black | streaming | photo -> people blacked out -> chat or done dir (`CENSOR_BLACK_WATCH` adds a folder dock) |
| restore | streaming | photo -> people blurred, then the LLM repaints the background (`RESTORE_WATCH` adds a folder dock) |
| sort | batch | classifies images IN PLACE in `_inbox/` the moment they land (Gemini -> prim name + EXIF fandom + week tag; CLIP decides instantly when Gemini punts); the working week stays in `_inbox/` |
| catch | streaming | new Downloads image -> prim-named copy straight into `pictures/<Fandom>/`; the original never leaves `CATCH_DIR` |
| week-clean | batch | Monday, mechanical: strip the week tag, shelve each classified image into `pictures/<Fandom>/` per its EXIF; unclassified files stay for retry |
| print | streaming | PDF in `print/` -> spooler -> `print/_done/` (`PRINT_SPOOLER`: lp / SumatraPDF) |
| kindle | outlier | Apps Script, `deploy/apps_script/` |

Telegram contract: each bot is its own Telegram identity
(`TG_TOKEN_<BOT>`), and files cross Telegram as documents only, both
directions -- compressed photos/videos are ignored with a logged
reason, results come back via sendDocument (never recompressed).

## CI gates

`ruff check` (select=ALL), `ruff format --check`, `mypy` (strict),
`pytest` (includes the ASCII gate and the import-boundary analysis),
wheel build. All green on main.
