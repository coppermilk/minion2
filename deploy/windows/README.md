# Windows deployment (BLUEPRINT 14: Task Scheduler, bare Python)

Windows runs exactly two bots: **print** (the printer is attached
here) and **catch** (the Downloads watcher). Everything else --
Telegram bots, sort, week-clean -- runs in Docker on the NAS
(`docker compose up -d` at the repo root); do not run those here or
they will run in two places.

Three steps:

1. Install Python 3.12+ and the package from the repo root:
   `pip install -e .[ml,llm,links,tg]` -- the full runtime stack
   (torch/transformers for vision, google-genai, yt-dlp, telethon). The
   launcher `run.ps1` installs the same set for you and re-runs it
   whenever `pyproject.toml` changes.
2. Copy `.env.example` to `.env` at the repo root -- the **same**
   single `.env` the NAS uses works verbatim (paths are validated
   for either OS). The Windows-relevant lines:
   - `DRIVE` -- absolute path to the mapped Drive folder
     (e.g. `C:\Users\a\My Drive`); relative paths refuse to start
     (REQ-CFG-001).
   - `PRINT_SPOOLER=C:\Apps\SumatraPDF.exe;-print-to-default;-silent`
     -- the print bot's spooler axis (REQ-PRT-001).
   - `CATCH_DIR=C:\Users\a\Downloads` -- the catch bot's source.
   - `GEMINI_API_KEY` -- catch uses it to classify images.
3. Task Scheduler -> Create Task -> Trigger **At log on** ->
   Action **Start a program**, and paste one line:

   ```
   Program:   powershell
   Arguments: -NoProfile -ExecutionPolicy Bypass -File C:\path\to\minion2\deploy\windows\run.ps1
   ```

   That is the whole setup: `run.ps1` loads `.env` and launches both
   bots. Nothing else to register.

Verify in `DRIVE\bots\_data\logs\`:
- `<bot>.log` -- runtime events (every disposition with its reason
  code);
- `<bot>.launcher.log` / `.err` -- startup problems that happen
  before the bot's own logger exists (a `bad_config` refusal, a
  missing python). If a bot "silently does nothing", the answer is
  here.

Idle cost is near zero by design: both bots sleep in folder waits and
the write-stability guard keeps half-downloaded files untouched.

## Generating the aggregator session on Windows

The **aggregator** userbot runs on the NAS (Docker), but its Telethon
login is easiest to do here, on a machine where entering the phone code
and 2FA is convenient. You generate the session **file** once and hand it
to the NAS -- no container rebuild.

1. Install the package with the telethon extra:
   `pip install -e .[tg]` (from the repo root). If you already run the
   print/catch launcher (`run.ps1`), this is done for you -- it installs
   the full `.[ml,llm,links,tg]` stack and re-runs whenever
   `pyproject.toml` changes.
2. Put `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` (from
   <https://my.telegram.org>) in the repo-root `.env`, and make sure
   `DRIVE` points at your Google Drive folder (e.g.
   `DRIVE=C:\Users\a\My Drive`).
3. Log in once -- it asks for phone, code, and 2FA if enabled:

   ```
   python -m minions.aggregator.login
   ```

   It creates the session at **`%DRIVE%\bots\aggregator\telethon.session`**
   (right in your Google Drive) and prints the exact path. The aggregator
   reads it from that same place, so on Windows there is nothing else to do.
4. To run the aggregator on the **NAS** instead, copy that file **as-is**
   (no rename) to `<DRIVE_NAS>/bots/aggregator/telethon.session` -- the
   host folder compose mounts as `/data` (check `DRIVE_NAS` in `.env`).
   The container defaults the session to `<DRIVE>/bots/aggregator/`
   (DRIVE=/data there), so the name and layout match. Then
   `docker compose up -d aggregator` logs in silently.

> The `.session` file is full account access -- it is git-ignored; don't
> commit or share it, and revoke it from Telegram -> Settings -> Devices
> if it leaks.
