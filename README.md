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
cp .env.example .env       # fill DRIVE (absolute), TG_TOKEN, TG_CHATS
pip install -e '.[dev]'
pytest                     # hermetic: no network, no models
python -m minions.inbox.main
```

Docker (NAS): set `DRIVE_NAS` in `.env`, then
`docker compose up -d`. Batch bots (sort, week-clean) run under the
foreground cron of the `batch` container.

## The bots

| Bot | Kind | Behaviour |
|-----|------|-----------|
| inbox | streaming | Telegram file -> `_inbox/` |
| fetch | streaming | link -> video (sink: chat / fan queue) |
| frames | streaming | video/link -> every Nth frame -> chat |
| censor | streaming | photo -> people hidden -> chat |
| sort | batch | images -> `pictures/<Fandom>/` (4 passes) |
| week-clean | batch | Monday: strip weekly EXIF tag, clear `_inbox/` |
| print | streaming | PDF in `print/` -> printer -> `print/_done/` |
| kindle | outlier | Apps Script, `deploy/apps_script/` |

## CI gates

`ruff check` (select=ALL), `ruff format --check`, `mypy` (strict),
`pytest` (includes the ASCII gate and the import-boundary analysis),
wheel build. All green on main.
