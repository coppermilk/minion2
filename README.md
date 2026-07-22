# Bananaland

A personal media pipeline: media items (photos, videos, links) come
in over two transports (Telegram, watched folders), each bot applies
exactly one small transformation, and everything lands in one
directory tree rooted at `DRIVE`.

- Per-bot reference (config, commands, admin knobs): [docs/BOTS.md](docs/BOTS.md)
- Architecture at a glance (one diagram): [ARCHITECTURE.md](ARCHITECTURE.md)
- Design, requirements, traceability: [BLUEPRINT.md](BLUEPRINT.md)
- Operations, failure modes, recovery: [OPERATIONS.md](OPERATIONS.md)
- Atomic web services (HTTP/OpenAPI + MCP over a Step): [services/README.md](services/README.md)
- Driving the services from n8n: [deploy/n8n/README.md](deploy/n8n/README.md)

## Layout

```
minion_core/   kernel (the belt), Settings, prompts, adapters
minions/       one directory per bot; streaming or batch; relay/ = thin transport
services/      atomic web services: HTTP/OpenAPI + MCP over a Step (bytes in/out)
tests/         requirement-based suite + structural analysis
docker/        base image; deploy/reactflow/ = canvas placeholder
deploy/        nas-update.sh, n8n workflow, kindle Apps Script (off-kernel)
```

## Quick start

```
cp .env.example .env       # DRIVE (absolute), TG_TOKEN_<BOT>, TG_CHATS
pip install -e '.[dev]'
pytest                     # hermetic: no network, no models
python -m minions.bots.inbox.main
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
| frames | streaming | video/link -> every 5th frame into `done/<MMDD> <clip>/` (clip filed in `_done/`), summary to chat (never the frames); **drop a video into `bots/frames/`** or set `FRAMES_WATCH` |
| censor-blur | streaming | photo -> people's silhouettes blurred (segmentation) -> chat + `done/<MMDD> <name>/` with the original in `_done/`; **drop a photo into `bots/censor-blur/`** or set `CENSOR_BLUR_WATCH` |
| censor-black | streaming | photo -> faces blacked out -> chat + `done/<MMDD> <name>/` with the original in `_done/`; **drop a photo into `bots/censor-black/`** or set `CENSOR_BLACK_WATCH` |
| restore | streaming | photo -> people blurred, then the LLM repaints the scene without them -> chat + a `done/` folder; **drop a photo into `bots/restore/`** or set `RESTORE_WATCH` |
| sort | batch | classifies images IN PLACE in `_inbox/` the moment they land (the active backend -> prim name + EXIF fandom + week tag; CLIP decides instantly when it punts); the working week stays in `_inbox/` |
| catch | streaming | new Downloads image -> prim-named copy straight into `pictures/<Fandom>/`; the original never leaves `CATCH_DIR` |
| week-clean | batch | Monday, mechanical: strip the week tag, shelve each classified image into `pictures/<Fandom>/` per its EXIF; unclassified files stay for retry |
| model-switch | streaming | moderator/admin panel (Telegram commands): `menu` shows the panel; `local`/`gemini`/`status` flip the classify+props backend at runtime, `clean` runs week-clean now, `bed`/`wishlist` read the donations+wishlist state |
| props | streaming | scenario (pasted, or the weekly script) -> recommended props, split into what the `Pr*` library has vs. still needs |
| donations | streaming | polls one or more donation platforms (Streamlabs + Revolut; `DONATION_PLATFORM` is a comma list, each on its own cursor) -> a Russian alert (who gave, how much, their question, linking that platform's tip page) posted to `DONATION_CHAT`; also a PUBLIC `/bed` command anyone can send to see who is "under the bed" (donors of the last 7 days), optionally auto-posted on a timer (`BED_BROADCAST_SEC`, `BED_CHAT`) |
| wishlist | batch | daily snapshot of a public Amazon wishlist; an item gone since yesterday is treated as a gift -> its photo + a Russian thank-you to `WISHLIST_CHAT` (cron cadence, like week-clean) |
| print | streaming | PDF in `print/` -> spooler -> `print/_done/` (`PRINT_SPOOLER`: lp / SumatraPDF) |
| kindle | outlier | Apps Script, `deploy/apps_script/` |

Model backend: classification and the props bot run behind one adapter
(`minion_core/adapters/backend.py`) over interchangeable models. The
default is **Gemini** (fast; a low-power NAS CPU can't run a 7B vision
model at usable speed). The `model-switch` bot flips to a **local
Qwen2.5-VL** (Ollama container "gem") and back at runtime; the exact
prompts sent to the model are logged. Background restore stays
Gemini-only (image generation). To use the local model, pull it once:
`docker compose exec ollama ollama pull qwen2.5vl:7b` (or `:3b`).

Telegram contract: each bot is its own Telegram identity
(`TG_TOKEN_<BOT>`), and files cross Telegram as documents only, both
directions -- compressed photos/videos are ignored with a logged
reason, results come back via sendDocument (never recompressed).

## Atomic services and the Telegram split

The processing IP and the Telegram transport are **fully separated**, in one
`docker-compose.yml`:

- **`svc-*`** -- one atomic web service per Step: bytes in, bytes out over
  HTTP (`/run-file`, async `/jobs/file`) and MCP. No Telegram. A folder
  result (frames) comes back as one zip. n8n, a React Flow canvas or an MCP
  agent call these the same way ([services/README.md](services/README.md)).
  Each is its own self-contained minion (`minions/<name>/step.py` owns the
  model, `minions/<name>/service.py` serves it); the container runs
  `python -m minions.<name>.service` and imports **only its own Step** -- no
  catalog, no sibling service, no Telegram.
- **`telegram`** -- ONE clean container that owns every media bot's Telegram
  identity and holds no processing code: each dock receives a file (or link),
  POSTs it to its service over HTTP (`minions/telegram/main.py` ->
  `minions.telegram.relay` -> `CallService`), and sends the bytes back.

So `censor-blur`, `censor-black`, `restore`, `frames`, `fetch` and `fan-save`
run as services (`svc-*`), and the `telegram` container is a thin router in
front of them -- no torch, no IP. The rest are not file-processors coupled to
Telegram, so they stay as they are: `inbox` (ingest), `model-switch`/`props`
(chat commands), `sort`/`batch` (folder watch + cron), `ollama` (local model).

## CI gates

`ruff check` (select=ALL), `ruff format --check`, `mypy` (strict),
`pytest` (includes the ASCII gate and the import-boundary analysis),
wheel build. All green on main.
