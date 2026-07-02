# Windows deployment (BLUEPRINT 14: Task Scheduler, bare Python)

One page, four steps:

1. Install Python 3.11+ and the package:
   `pip install -e .` from the repo root (add `.[ml,llm]` if this
   machine runs catch -- it needs the vision and naming adapters).
2. Copy `.env.example` to `.env` at the repo root and fill it in:
   - `DRIVE` -- absolute path to the mapped Drive folder
     (e.g. `C:\Users\a\My Drive`); relative paths refuse to start
     (REQ-CFG-001).
   - `PRINT_SPOOLER=C:\Apps\SumatraPDF.exe;-print-to-default;-silent`
     -- the print bot's spooler axis (REQ-PRT-001).
   - `CATCH_DIR=C:\Users\a\Downloads` -- the catch bot's source.
   - `TG_TOKEN_<BOT>` per Telegram bot; `TG_CHATS` allow-list.
3. From an elevated PowerShell:
   `deploy\windows\register-tasks.ps1`
   (sort every 5 minutes, week-clean Mondays 06:00, print and
   catch at logon; add more with `-Bots inbox,censor-blur`).
4. Verify: `schtasks /Query /TN bananaland\` and watch
   `DRIVE\bots\_data\logs\<bot>.log`.

Idle cost is by design near zero: sort exits immediately when there
is nothing to place (OPERATIONS 5), the embeddings cache is not
rewritten when the tree is unchanged, and streaming bots sleep in
long-poll/folder waits.

Remove everything with `deploy\windows\unregister-tasks.ps1`.
