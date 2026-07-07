# Bananaland -- Operations, Failure Modes, and Recovery

Companion to [BLUEPRINT.md](BLUEPRINT.md) (design, requirements, verification).
Ordered by consequence, highest first, using the criticality tiers defined
there. Three operating principles govern every procedure: **fail safe** (never
destroy MEDIA on error), **preserve STATE** (`state/` is sacred, `regen/` is
not), **stay observable** (every non-DELIVERED disposition is logged with a
reason code -- REQ-OBS-001).

## 1. Data preservation (CT-A -- the irreversible tier)

- **Never delete `bots/_data/state/<bot>.offset`** (REQ-DATA-003). It is the
  bot's Telegram high-water mark; deleting it replays old messages
  (re-download, re-censor, re-reply). Backup policy in one line: back up
  `state/`, ignore `regen/`.
- **`bots/_data/regen/` is CACHE.** Weights + `_embeddings.npz`; `TORCH_HOME`
  and the HF cache point here so weights survive restarts. Wipe at any idle
  moment to reclaim space; never mid-run.
- **Disposal is ordered after delivery** (REQ-KRN-004): a FAILED or REJECTED
  verdict leaves the source file in place. Output never overwrites
  (REQ-DATA-001, collisions -> `_2`, `_3`); writes are atomic (REQ-DATA-002).

## 2. Failure modes and effects (reason codes match `Verdict.reason`)

| Reason code | Failure mode | Symptom | Tier | Recovery / action |
|-------------|--------------|---------|------|-------------------|
| `offset_lost` | offset file deleted/corrupt | bot re-processes old messages | CT-A | restore `state/` from backup; else set offset to the current high-water mark before restart |
| `quota_exceeded` | volume near cap | downloads REJECTED pre-transfer or aborted mid-stream | CT-A | free MEDIA space or raise `quota_bytes`; the two-sided check (REQ-RES-002) prevents a full volume by construction |
| `name_collision` | (auto-resolved) | files gain `_2`, `_3` suffixes | CT-A | none needed; investigate only if unexpected duplicates flood in |
| `ssrf_blocked` | link resolves to a private/reserved host | fetch REJECTED pre-connect | CT-B | expected behaviour (REQ-SEC-001); do not whitelist |
| `stale_extractor` | yt-dlp extractors rotted | downloads FAILED as sites change | CT-C | restart the container (`yt-dlp -U` runs on start) or refresh the pinned binary on schedule |
| `download_timeout` | hung extractor/host | FAILED after `download_timeout_sec` | CT-C | none; the bound exists so the daemon never wedges (REQ-RES-001) |
| `probe_failed` | ffmpeg/ffprobe missing or both probe paths failed | frame jobs FAILED | CT-C | ship binaries in `bin/` or let the image apt-install ffmpeg |
| `batch_locked` | second batch invocation while one runs | second run exits immediately | CT-C | none; this is REQ-RES-003 working. A lock is auto-reaped only when its holder died on the same host; a lock orphaned by a RECREATED container names a foreign host -- delete `state/<bot>.lock` by hand |
| `cache_wiped_live` | `regen/` deleted mid-run | current run FAILED on model load | CT-C | re-run when idle; the cache rebuilds unattended |
| `bad_config` | relative path override | process refuses to start, loud error | CT-C | make the override absolute (REQ-CFG-001); never relative |
| `bad_update` | malformed Telegram payload | update skipped, logged; offset advances past it | CT-C | none; explicit boundary validation -- a poison update can neither crash the dock nor wedge it in a replay loop |
| `hash_collision` | two coexisting images with equal digest+length but different bytes | both embedded on distinct keys; CRITICAL log | CT-B | none needed -- detected by direct byte comparison and split; correctness holds by construction |
| `classify_failed` | the active backend (local Qwen or Gemini) crashed, was unreachable (`ollama_unreachable`), had no model pulled (`model_not_pulled`), or returned garbage | catch job FAILED / sort leaves the file in its source dir | CT-B | none needed; the file is retried on the next run (REQ-CATCH-002) -- if it persists, check the model (`docker compose logs ollama`, or `GEMINI_API_KEY`/model id when switched to Gemini) |
| `demote_failed` / `replace_failed` | a sort library-hygiene pass crashed (a corrupt image, a torch/CLIP load) | that pass is skipped for the run; classify's per-image results already landed | CT-B | none; contained by design and retried next run -- the full traceback is in `sort.log` (no longer an opaque `step_crashed`) |
| `script_fetch_failed` | weekly script doc unreachable or not shared "anyone with the link" | classification proceeds without scene labels | CT-D | re-share the doc publicly and drop a fresh `.gdoc` shortcut into `_inbox/` |
| `no_person` | censor-blur/restore detector found no person | job SKIPPED; the original is NOT sent back | CT-B | expected: a silent pass-through would leak an uncensored photo |
| `no_face` | censor-black found no face | job SKIPPED; the original is NOT sent back | CT-B | expected (same leak-safety as `no_person`); if a face was clearly present, the angle/occlusion beat the detector -- re-send a clearer crop |
| `restore_failed` | image model returned no repaint | restore job FAILED; the `_s1` blur stays in the work dir | CT-B | retry later or check `GEMINI_API_KEY`/model id |
| `not_a_document` | compressed photo/video sent to a bot | payload ignored, logged | CT-D | re-send the file as a document; Telegram recompresses anything else (the documents-only contract) |
| `bad_image` | non-image bytes under an image extension | job REJECTED; file left in place | CT-B | expected: untrusted input validated explicitly (BLUEPRINT 4) |
| `printer_missing` | spooler binary absent | print jobs FAILED | CT-C | fix `PRINT_SPOOLER` (REQ-PRT-001) or install the spooler |
| `print_timeout` | hung spooler | FAILED after `print_timeout_sec` | CT-C | none; the bound exists so the daemon never wedges |
| `print_failed` | spooler returned non-zero | print jobs FAILED | CT-C | check the printer/CUPS queue; the PDF stays in `print/` for the retry after restart |
| `step_crashed` | unclassified fault inside a Step | job FAILED with a stack trace in the log | CT-C | this is the crash guard working (REQ-KRN-001); file a defect for the missing stable code |

## 3. Link fetching (fetch adapter)

Extractors rot within weeks, so the volatile knobs are Settings, not code:

- **Knobs** (env var + restart, no code edit): `ytdlp_format` (default
  best-video+best-audio), `ytdlp_container` (default `mkv`),
  `ytdlp_player_clients` (the throttle / age-gate dodge). The YouTube
  `player_client` extractor-arg is attached only when the URL host is YouTube.
- **Invariants** (in code, deliberately not knobs): one link -> one file
  (`--no-playlist`); trust the downloader's own final path
  (`--print after_move:filepath`) -- the output name is never guessed.
- **Bounds always on**: wall-time (`download_timeout_sec`), disk
  (`quota_bytes`, before + mid-stream for direct transfers), host set (SSRF
  guard rejects loopback / link-local / RFC-1918 / reserved pre-connect; the
  chat allow-list remains the primary control, the guard is defence in depth).
- **Self-update on start**: link-bot containers run `yt-dlp -U` before launch.

## 4. Sort cache (vision adapter)

- **Append-only, verified**: `_embeddings.npz` keys vectors by
  `SHA-256:length` of the image bytes, and identity is never taken on faith
  among coexisting files: a repeated key is settled by comparing the bytes
  themselves -- equal bytes legitimately share one vector, unequal bytes are
  a detected collision, split onto distinct keys with a CRITICAL
  `hash_collision` log. Wrong-vector service is therefore impossible for
  every checkable case; the sole unwitnessable case (deleted file, later a
  different one with the same digest and length) sits at ~2^-256 and cannot
  be verified by any bounded key without storing full content copies. An
  image embeds once in its life; moves and renames reuse the vector; the
  scan stays capped at `max_embedding_scan`.
- **Demote never invalidates** (REQ-SORT-001 restated): the fandom mapping is
  rebuilt from the live tree on every refresh and never persisted, so
  Re-place matches the new layout by construction -- ghosts are impossible
  and a demoted library costs zero re-embeds. `invalidate()` remains as a
  manual recovery tool only (the cache is CACHE-class: wipe at any idle
  moment).
- **Shared across containers**: every container mounts the same `/data`, so
  there is exactly one `_embeddings.npz`. Writes are atomic (temp + replace),
  a reader never sees a torn file, and `refresh` rebuilds its key set from
  the live tree -- a stale cache cannot resurrect ghosts. Two concurrent
  writers (sort's cron run and catch) degrade to last-writer-wins: the
  loser's vectors are recomputed next run; never corruption. On Drive-synced
  deployments a sync conflict copy may simply be deleted -- the cache is
  CACHE-class data.
- **No idle rewrites**: an unchanged tree writes nothing; the npz is only
  rewritten when a vector was computed or the key set changed, so the
  5-minute cron and Drive sync cost nothing while asleep.
- **One-time after upgrading to pooled embeddings**: `embed_image` now pools
  any stray multi-dim model output to a flat vector. If an older cache holds a
  differently-shaped vector, delete `regen/_embeddings.npz` once (CACHE-class,
  it rebuilds unattended) so every vector is consistent.

## 4a. Folder drops (censor-blur / censor-black / restore / frames)

Each of these bots always watches its OWN folder: drop a photo (or a video,
for frames) straight into `bots/<bot>/` on the media share and it is processed
into `bots/<bot>/done/<MMDD> <name>/`, no Telegram token and no config needed.
`<BOT>_WATCH` still overrides the watched dir. Telegram downloads and
intermediate `_s1`/`_s2` files live in `bots/<bot>/_spool/` (a subfolder the
watcher ignores), so a dropped file is picked up exactly once and results are
never re-processed.

## 5. Scheduling and liveness

- **Batch bots** (sort, week-clean) are one-shot: scan, act, exit. Cadence
  belongs to cron; the container runs `cron -f` foreground as the supervisor.
  An idle run exits fast -- sort returns before touching the cache or any
  adapter when there is nothing to place and nothing in `Unknown/` -- and
  the per-bot lock makes overlap impossible. The Windows machine runs only
  print and catch, at logon via `deploy/windows/run.ps1`; every
  other bot lives in Docker so each bot runs in exactly one place.
- **Instant sorting** (`SORT_WATCH=1`): sort runs as a watch daemon -- one
  Folder dock per source dir triggers a locked pass run the moment a new
  stable image lands (~`POLL_SEC` latency; the write-stability guard keeps
  half-written files out). The batch lock makes the daemon and any manual
  one-shot run safe together; week-clean stays on the calendar.
- **Streaming bots** run 24/7 under `restart: unless-stopped`.
- **Liveness definition**: the container is up AND the offset is advancing.
  A crash is a supervised restart; the restart is safe because STATE is on
  disk (section 1).

## 6. Naming

Two naming domains, one file each:

- **Transport** (`files.stem`): `MMDD_<source>_<name>.<ext>` (`source` is
  `tg` or `loc`); two-step bots append `_s1` (intermediate) / `_s2` (final);
  collisions resolve via `next_free_path` (`_2`, `_3`, ...). The sender's
  original name is preserved (`files.sanitize` keeps Unicode letters,
  spaces and brackets -- Cyrillic survives; only path separators, control
  and Windows-reserved chars are stripped) and NEVER lost except by the
  library classifier. Extracted frames are `<timecode>_<video name>.jpg`
  where the timecode drops empty leading fields -- `5-25` under a minute,
  `1-05-325` past a minute, `1-01-05-3900` past an hour (frame = source
  frame index, a multiple of 5).
- **Output folders** (`files.Shelve`, `files.dated_dir`): every archiving
  bot files its result(s) into `bots/<bot>/done/<MMDD> <name>/` and moves
  the consumed original into that folder's `_done/` (kept, not deleted) --
  frames, censor-blur/black, restore and fan-save all follow this. censor
  and restore still send the result to the chat; frames replies a summary
  only (never the frames).
- **Library** (`files.usd_prim`): everything sort/catch classify is a valid
  OpenUSD prim identifier -- UpperCamelCase, letters and digits only, layer
  prefix first (`Bg|Fg|Ov|Pr|Tx`), e.g. `FgSnapeOfficeAngry.jpg`; collisions
  take a bare digit suffix via `next_free_prim` (`FgSnapeOfficeAngry2.jpg`)
  so the name stays a prim. The Gemini verdict supplies the name; a
  `censored=true` verdict is logged (`censored=True` on the `classified`
  line) but changes nothing else.

The working week: sort classifies IN PLACE -- the file is renamed to its
prim in `_inbox/`, the fandom goes into EXIF ImageDescription
(`files.tag_fandom`, because the prim name deliberately carries no fandom)
and the weekly tag marks the working set. Everything is decided during the
week: when Gemini punts (Unknown), CLIP picks the nearest library fandom in
the same instant run. Monday's week-clean is purely mechanical -- strip the
weekly tag, move each classified file into `pictures/<Fandom>/` per its
EXIF (a non-JPEG cannot carry EXIF and lands in `Unknown/`, where Re-place
rescues it). Unclassified leftovers are never deleted -- they wait for the
next attempt. catch is the exception: its copy goes straight into the
library (the original stays in Downloads anyway).

Weekly script hints ride the same tree: drop the week's Google Doc `.gdoc`
shortcut(s) into `_inbox/` (docs shared "anyone with the link"); the first
sort/catch run consumes them (fetch + delete) and archives the text under
`Scripts/`, where every later run reads it for the rest of the week.

## 7. Configuration and tests in operation

- **Precedence is the mapping you pass**: production builds `Settings` from
  `os.environ` (a container's `DRIVE=/data` wins because it is there); tests
  pass a plain dict. There is no import-time global to fight; a relative path
  is rejected loudly at load (REQ-CFG-001), not discovered later.
- **The suite is the flight path**: bots run through the same kernel with
  doubles only at adapter boundaries; offline (lazy ML imports -- no torch
  needed), hermetic (each test builds Settings over a tmp_path), and fast.

## 8. Model backend (local Qwen / Gemini)

Classification and the props bot run behind one adapter
(`adapters/backend.py`) over interchangeable models; restore is Gemini-only
(image generation) and never routes through the toggle.

- **Default is local, offline.** The `ollama` container ("gem") runs
  Qwen2.5-VL; `MODEL_BACKEND` defaults to `local`. The model is pulled
  automatically by `deploy/nas-update.sh` (it reads `OLLAMA_MODEL` and runs
  `ollama pull` after the stack is up, idempotently) -- you never run it by
  hand. Weights live in the `ollama-models` named volume (Docker auto-creates
  it -- a bind to a missing host subpath would fail the start); it is CACHE
  class (re-pullable) and `nas-update`'s `image prune` never touches volumes.
- **Switch at runtime.** Message the `model-switch` bot `local`, `gemini`,
  or `status`. It writes `state/model.backend`; sort/catch/props re-read it
  per item, so the swap takes effect on the next image with no restart. The
  toggle is per-machine (each host's own `DRIVE/state`). `model-switch` and
  `props` each need their own `TG_TOKEN_*`; without it the tokenless dock
  ends and the container idle-restarts (harmless, but that is the repeating
  `drained` line in its log).
- **DS224+ latency.** The Celeron J4125 has no AVX2 and no GPU, so one local
  classify runs in minutes. Fine for the trickle sort's watch daemon sees. A
  model that is down (`ollama_unreachable`) or not yet pulled
  (`model_not_pulled`, which names the exact `ollama pull` command) just
  leaves images waiting in `_inbox` (the existing punt), never a crash. Every
  bot's full log -- including kernel crash tracebacks -- is mirrored to the
  container's stdout, so `docker compose logs <bot>` (Container Manager's Log
  tab) is the complete view.
- **Memory.** The 7B model stays resident (~6 GB on the 16 GB NAS). Keep the
  sum of simultaneous peaks under ~14 GB -- the torch bots (censor/sort) load
  their models only while working, so real concurrency is low.
- **catch on Windows.** The `ollama` service is NAS-side. For catch (a
  Windows bot) to classify locally, point `OLLAMA_URL` at the NAS's Ollama
  (expose the port) or switch that host to Gemini; otherwise catch's images
  wait until a backend is reachable.
