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
| `batch_locked` | second batch invocation while one runs | second run exits immediately | CT-C | none; this is REQ-RES-003 working -- a tight cron schedule is safe because of it |
| `cache_wiped_live` | `regen/` deleted mid-run | current run FAILED on model load | CT-C | re-run when idle; the cache rebuilds unattended |
| `bad_config` | relative path override | process refuses to start, loud error | CT-C | make the override absolute (REQ-CFG-001); never relative |
| `classify_failed` | LLM naming or vision placement crashed | catch job FAILED; file untouched in `catch_dir` | CT-B | none needed; the belt continues (REQ-CATCH-002) -- investigate the adapter if it persists |
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

- **Incremental**: `_embeddings.npz` keys vectors by `(path, fandom)`;
  recomputes only new/changed files, drops removed ones, caps the scan at
  `max_embedding_scan` -- re-running sort is cheap by design.
- **Invalidate after Demote** (REQ-SORT-001): Demote moves sparse fandoms to
  `Unknown/` and clears the cache so Re-place matches the new layout, not
  ghosts of the old one. Skipping this is a silent-misplacement defect, not an
  optimization.

## 5. Scheduling and liveness

- **Batch bots** (sort, week-clean) are one-shot: scan, act, exit. Cadence
  belongs to cron; the container runs `cron -f` foreground as the supervisor.
  An idle run exits fast, and the per-bot lock makes overlap impossible, so a
  tight schedule (every 5 min) is cheap and safe.
- **Streaming bots** run 24/7 under `restart: unless-stopped`.
- **Liveness definition**: the container is up AND the offset is advancing.
  A crash is a supervised restart; the restart is safe because STATE is on
  disk (section 1).

## 6. Naming

Stems come from `files.stem`: `MMDD_<source>_<name>.<ext>` (`source` is `tg` or
`loc`); two-step bots append `_s1` (intermediate) / `_s2` (final); collisions
resolve via `next_free_path`.

## 7. Configuration and tests in operation

- **Precedence is the mapping you pass**: production builds `Settings` from
  `os.environ` (a container's `DRIVE=/data` wins because it is there); tests
  pass a plain dict. There is no import-time global to fight; a relative path
  is rejected loudly at load (REQ-CFG-001), not discovered later.
- **The suite is the flight path**: bots run through the same kernel with
  doubles only at adapter boundaries; offline (lazy ML imports -- no torch
  needed), hermetic (each test builds Settings over a tmp_path), and fast.
