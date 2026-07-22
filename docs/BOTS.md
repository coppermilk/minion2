# Bots

One directory per bot under `minions/bots/<name>/`. Each bot is either a
**streaming** dock (a long-lived process draining a belt) or a **batch**
run (one scan-act-exit, fired by cron). Secrets and deploy wiring live in
the environment (`.env`); operator-tunable knobs live in a runtime config
file the moderator edits from chat (see [Admin config](#admin-config)).

Every bot degrades cleanly: with no token (or nothing configured) it ends
as a no-op instead of crashing (REQ-DEG-001).

| Bot | Kind | One line |
|-----|------|----------|
| [inbox](#inbox) | streaming | Telegram file -> `_inbox/` |
| [fetch](#fetch) | streaming | link -> video (chat / fan queue) |
| [fan-save](#fan-save) | streaming | link -> video parked for later |
| [frames](#frames) | streaming | video/link -> every Nth frame |
| [censor-blur](#censor-blur) | streaming | photo -> people blurred |
| [censor-black](#censor-black) | streaming | photo -> faces blacked out |
| [restore](#restore) | streaming | photo -> people removed, scene repainted |
| [sort](#sort) | batch/watch | classify images in place in `_inbox/` |
| [catch](#catch) | streaming | new Downloads image -> `pictures/<Fandom>/` |
| [week-clean](#week-clean) | batch | Monday: shelve the classified week |
| [model-switch](#model-switch-the-moderator) | streaming | the moderator / admin panel |
| [props](#props) | streaming | scenario -> recommended props |
| [donations](#donations) | streaming | donor alerts + the "under the bed" game |
| [wishlist](#wishlist) | batch | daily wishlist diff -> gifts and new wants |
| [print](#print) | streaming | PDF in `print/` -> spooler |
| [kindle](#kindle) | outlier | Apps Script, off-kernel |

The media-pipeline bots (inbox..catch, print, kindle) are covered in
depth by [BLUEPRINT.md](../BLUEPRINT.md) and [OPERATIONS.md](../OPERATIONS.md);
the sections below give each a short entry and document the streamer bots
(donations, wishlist) and the moderator in full.

---

## Admin config

Runtime knobs live in `bots/_data/state/admin.json`, written by the
moderator bot from chat and read by every bot at its natural point (a
streaming bot each loop, a batch bot at the start of a run). No redeploy
is needed to change a schedule or flip a toggle.

Settings (the registry in `minion_core/adapters/admin.py`):

| Key | Default | Meaning |
|-----|---------|---------|
| `donation_platform` | `streamlabs` | donations: platforms, comma list (`streamlabs,revolut`) |
| `donation_chat` | (blank) | donations: alert channel (blank = env `DONATION_CHAT`) |
| `donation_poll_sec` | `10` | donations: seconds between feed polls (restart to apply) |
| `bed_broadcast_sec` | `0` | donations: bed auto-post interval in seconds (0 = off) |
| `bed_chat` | (blank) | donations: chat for the bed auto-post (blank = `donation_chat`) |
| `wishlist_url` | (blank) | wishlist: the public wishlist URL (blank = env `WISHLIST_URL`) |
| `wishlist_chat` | (blank) | wishlist: gifts/adds channel (blank = env `WISHLIST_CHAT`) |
| `wishlist_enabled` | `1` | wishlist: run the daily scan (1/0) |
| `wishlist_announce` | `0` | wishlist: post the full list every run (1/0) |
| `week_clean_enabled` | `1` | week-clean: run the Monday shelving (1/0) |

Edit them with the moderator commands `config`, `set <key> <value>`,
`get <key>`, `reset <key>` (see [model-switch](#model-switch-the-moderator)).

Precedence: an explicit moderator override wins; otherwise a non-secret
env var (e.g. `DONATION_CHAT`, `WISHLIST_URL`) seeds the value, else the
default. Only secrets -- the `TG_TOKEN_*`, `STREAMLABS_TOKEN` and
`REVOLUT_TOKEN` -- stay env-only.

---

## inbox

- **Kind:** streaming. **Token:** `TG_TOKEN_INBOX`.
- Every Telegram document lands in `_inbox/`. Compressed photos/videos are
  refused with a logged reason (documents-only contract).

## fetch

- **Kind:** streaming. **Token:** `TG_TOKEN_FETCH`.
- A link becomes a downloaded video; the sink is the chat or the fan
  queue (`FETCH_SINK`).

## fan-save

- **Kind:** streaming. **Token:** `TG_TOKEN_FAN_SAVE`.
- A link (TikTok/YouTube/...) is parked as a video in
  `bots/fan-save/done/<MMDD> <title>/` for later work.

## frames

- **Kind:** streaming. **Token:** `TG_TOKEN_FRAMES`.
- A video or link yields every Nth frame into `done/<MMDD> <clip>/`; a
  summary (never the frames) goes to chat. Drop a video into
  `bots/frames/` or set `FRAMES_WATCH`.

## censor-blur

- **Kind:** streaming. **Token:** `TG_TOKEN_CENSOR_BLUR`.
- A photo comes back with people's silhouettes blurred (segmentation).
  Drop a photo into `bots/censor-blur/` or set `CENSOR_BLUR_WATCH`.

## censor-black

- **Kind:** streaming. **Token:** `TG_TOKEN_CENSOR_BLACK`.
- A photo comes back with faces blacked out. Drop a photo into
  `bots/censor-black/` or set `CENSOR_BLACK_WATCH`.

## restore

- **Kind:** streaming. **Token:** `TG_TOKEN_RESTORE`.
- People are blurred, then the LLM repaints the scene without them
  (Gemini-only). Drop a photo into `bots/restore/` or set `RESTORE_WATCH`.

## sort

- **Kind:** batch / watch (`SORT_WATCH=1`).
- Classifies images in place in `_inbox/` the moment they land: prim name
  + EXIF fandom + week tag. The working week stays in `_inbox/`.

## catch

- **Kind:** streaming. **Config:** `CATCH_DIR`.
- A new Downloads image is copied, prim-named, into `pictures/<Fandom>/`;
  the original never leaves `CATCH_DIR`.

## week-clean

- **Kind:** batch (cron: Monday). **Admin:** `week_clean_enabled`.
- Mechanically shelves the classified week: strip the week tag, move each
  image into `pictures/<Fandom>/` per its EXIF. Nothing unclassified is
  touched and nothing is deleted. The cron fires it; the bot skips the run
  when `week_clean_enabled=0`. On demand, the moderator's `clean` command
  runs the exact same routine.

## model-switch (the moderator)

- **Kind:** streaming. **Token:** `TG_TOKEN_MODEL_SWITCH`.
- The admin panel: text commands that control the system and read each
  bot's state (it shares `/data`, so it reads other bots' STATE files).

Commands:

| Command | Effect |
|---------|--------|
| `menu` / `help` | print the whole panel |
| `local` / `gemini` | switch the classify/props model backend |
| `status` | which backend is live |
| `clean` | run week-clean now (the Monday routine) |
| `config` | list every setting with its value and help |
| `set <key> <value>` | change a setting (persisted to `admin.json`) |
| `get <key>` | read one setting |
| `reset <key>` | restore a setting to its default |
| `bed` | who is under the donations bed (last 7 days) |
| `wishlist` | how many items the wishlist bot tracks |
| `whois <chat_id>` | resolve a chat id to its name (via getChat) |

Only the chat allow-list (`TG_CHATS`) may drive the moderator.

## props

- **Kind:** streaming. **Token:** `TG_TOKEN_PROPS`.
- A scenario (pasted or the weekly script) yields recommended props,
  split into what the `Pr*` library already has vs. still needs.

## donations

- **Kind:** streaming. **Token:** `TG_TOKEN_DONATIONS`.
- Posts a Russian alert for each new donation and runs the "under the
  bed" game. Three docks on one belt: the alert poller, a public command
  dock, and a timed broadcast.

**Feeds (platform-agnostic).** `DONATION_PLATFORM` is a comma list, so
Streamlabs and Revolut run together, each on its own persisted cursor:

- `streamlabs` -> polls the REST donations API. `STREAMLABS_TOKEN` is the
  dashboard's "Your API Access Token" (not the Socket API Token).
- `revolut` -> polls Revolut Business incoming transactions.
  `REVOLUT_TOKEN` is a Bearer access token; those are short-lived, so a
  long deployment refreshes it out of band (OAuth refresh is out of
  scope). Only completed, incoming (credit) transactions count.

Each alert names the donor, the amount with its currency symbol, and the
message, links that platform's own tip page, and draws the donor into an
ASCII "bed". Adding a platform is one `Feed` class plus one line in
`feed_for`.

**The bed game.** Every donor is kept "under the bed" for 7 days
(`BedRoster`, in STATE; a fresh donation refreshes the timer). Anyone can
send the PUBLIC command `/bed` (or "kto pod krovatyu") in any chat to see
who is currently under the bed. Optionally the roster is auto-posted on a
timer -- `bed_broadcast_sec` (interval) and `bed_chat` (target, defaults
to `DONATION_CHAT`), both live admin knobs; an empty bed is never posted.

**Config:** `DONATION_CHAT` (env, the alert channel), `DONATION_PLATFORM`
(env), tokens (env); `donation_poll_sec`, `bed_broadcast_sec`, `bed_chat`
(admin). Add the bot to the target channel as an admin.

## wishlist

- **Kind:** batch (cron: daily). **Token:** `TG_TOKEN_WISHLIST`.
- Once a day it snapshots a public Amazon wishlist (`WISHLIST_URL`,
  walked across lek pages) and diffs it against yesterday's snapshot (the
  database, in STATE):
  - an item **gone** since yesterday is a **gift** -> its photo and a
    Russian thank-you to `WISHLIST_CHAT`;
  - an item **newly added** -> its photo and one of several blurbs picked
    at random (the templates are a JSON list; the code is agnostic to how
    many there are).
- Both messages are note-aware: each template has a with-note and a
  no-note form, chosen by whether the item carries an owner comment. Long
  card titles are trimmed to five words.
- The **first run** posts a digest of the current list (so you see what is
  watched) and saves a baseline; it announces no gifts/adds.
- **Safety:** a blocked or failed fetch (or an empty parse, or any failed
  lek page) keeps the old snapshot and posts nothing -- a block is never
  mistaken for a hundred gifts.
- **Config:** `WISHLIST_URL`, `WISHLIST_CHAT` (env); `wishlist_enabled`,
  `wishlist_announce` (admin -- the cron fires the run, the bot honors the
  toggle). Public wishlist scraping is best-effort: an Amazon anti-bot
  page may need cookies/a proxy, and the selectors may need tuning.

## print

- **Kind:** streaming (Windows). **Config:** `PRINT_SPOOLER`.
- A PDF in `print/` goes to the spooler (`lp` / SumatraPDF), then to
  `print/_done/`.

## kindle

- **Kind:** outlier (Google Apps Script, off-kernel). See
  `deploy/apps_script/`.
