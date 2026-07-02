# Windows deployment (BLUEPRINT 14: Task Scheduler, bare Python)

Windows runs exactly two bots: **print** (the printer is attached
here) and **catch** (the Downloads watcher). Everything else --
Telegram bots, sort, week-clean -- runs in Docker on the NAS
(`docker compose up -d` at the repo root); do not register those
here or they will run in two places.

Four steps:

1. Install Python 3.11+ and the package from the repo root:
   `pip install -e .[ml,llm]` (catch needs the vision and naming
   adapters; print alone needs only the base install).
2. Copy `.env.example` to `.env` at the repo root and fill the
   Windows-relevant lines (the TG_TOKEN_* block belongs to the
   Docker side and may stay empty here):
   - `DRIVE` -- absolute path to the mapped Drive folder
     (e.g. `C:\Users\a\My Drive`); relative paths refuse to start
     (REQ-CFG-001).
   - `PRINT_SPOOLER=C:\Apps\SumatraPDF.exe;-print-to-default;-silent`
     -- the print bot's spooler axis (REQ-PRT-001).
   - `CATCH_DIR=C:\Users\a\Downloads` -- the catch bot's source.
   - `GEMINI_API_KEY` -- catch uses it to name images.
3. From an elevated PowerShell:
   `deploy\windows\register-tasks.ps1`
   (print and catch at logon; extras only via `-Bots`, and only if
   you removed them from compose first).
4. Verify: `schtasks /Query /TN bananaland\` and watch
   `DRIVE\bots\_data\logs\<bot>.log`.

Idle cost is near zero by design: both bots sleep in folder waits,
catch embeds an image at most once in its life (identity-keyed
cache), and the write-stability guard keeps half-downloaded files
untouched.

Remove everything with `deploy\windows\unregister-tasks.ps1`.
