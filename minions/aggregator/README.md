# Aggregator (Telethon userbot)

A **Telethon** userbot -- a real user account over MTProto, **not** a Bot API
bot -- that aggregates one Short's links across platforms and posts the
collected message (with **premium / custom emoji**) to one or more chats.

It listens to a **source chat** where a bot (IFTTT/Zapier/etc.) drops one JSON
object per video per platform:

```json
{"platform": "youtube", "caption": "...", "link": "https://...",
 "thumnailUrl": "https://...jpg", "duration": "0:0:16"}
```

Messages whose captions match ~90% are treated as the same video. Once every
expected platform has arrived (or a timeout elapses), **one** message collecting
each platform's link is posted to the **target chat(s)**.

## Why a userbot and not a bot

Premium (custom) emoji can only be **sent** by a user account with Telegram
Premium; the Bot API cannot. So this uses Telethon with a **user session**, not
a bot token. `premium_emoji.py` turns Bot-API-style `<tg-emoji emoji-id="...">`
markup into Telethon `MessageEntityCustomEmoji` entities (measured in UTF-16
code units, as Telegram requires).

## Files

| File | What it is |
|------|-----------|
| `main.py` | the aggregator itself (`python -m minions.aggregator.main`) |
| `premium_emoji.py` | premium-emoji entity builder (`RichText`, `build_post_with_bar`, ...) |
| `aggregator_constants.json` | editable post texts + premium emoji ids (UTF-8) |
| `login.py` | log in once and write the session file (`python -m minions.aggregator.login`) |
| `dump_emoji_ids.py` | dev helper: print the id of every premium emoji you send |
| `greeter.py` | welcome/farewell DMs from the channel admin log (opt-in) |
| `users.py` | opt-in users database (SQLite): audience history + activity |

## Configuration

Two sources, no overlap: the **env** carries only the deploy knobs (credentials
and the chats); **`aggregator_constants.json`** carries all behaviour.

**env** (credentials + chats + paths):

| Env | Meaning |
|-----|---------|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | credentials (from <https://my.telegram.org>) |
| `TELEGRAM_PASSWORD` | optional 2FA/cloud password |
| `SOURCE_CHAT_ID` | the chat the per-platform JSON arrives in (monitoring) |
| `TARGET_CHAT_ID` | target chat(s) -- **comma-separated** to post to several |
| `TELEGRAM_SESSION_FILE` | session-file base path override (`.session` appended) |
| `AGGREGATOR_STATE_DIR` | state-dir override |
| `DRIVE` | library root; session + state default to `<DRIVE>/bots/aggregator/` |

**`aggregator_constants.json`** (behaviour + content):

| Key | Meaning |
|-----|---------|
| `platforms` | expected platforms, in priority order (comma-separated) |
| `title_match` | caption similarity to treat two messages as the same video |
| `timeout_sec` | how long to wait for the rest before posting a partial |
| `backfill` | recent source messages scanned at startup |
| `max_duration_sec` | a video at/above this many seconds is dropped (not a Short) |
| `repost_guard_sec` | do not re-post a video whose caption matches one already posted within this many seconds -- catches the source re-delivering the same video under new ids (an upstream re-emit, common with chat auto-delete); `0` disables it (default one week) |
| `fields`, `action_value`, `author`, `announce`, `love`, `lead_emoji`, `arrow_down`, `view_label`, `rows`, `platform_emoji` | incoming field names + the post's texts and premium emoji (`lead_emoji` leads the caption line) |

## Run (without Docker)

```bash
pip install -e '.[tg]'            # from the repo root
cp .env.example .env              # fill in TELEGRAM_API_ID / TELEGRAM_API_HASH
python -m minions.aggregator.main
```

The first run logs you in interactively (phone, code, 2FA if enabled) and writes
the session to `<DRIVE>/bots/aggregator/telethon.session` when `DRIVE` is set
(on Windows that is your Google Drive), else `telethon.session` next to this
package. Every run after that is silent, reading from the same place. A session
kept in the checkout is **git-ignored**, so it survives a repo re-sync
(`deploy/nas-update.sh`) -- exactly like `.env`.

## Run in Docker (this project's shared image)

The aggregator rides the **one shared image** (`telethon` is baked in via the
`tg` extra) -- there is no separate image to build. Compose (`aggregator`
service) mounts `${DRIVE_NAS}:/data`, sets
`TELEGRAM_SESSION_FILE=/data/bots/aggregator/telethon`, and reads `.env`.

From the **repository root**:

```bash
cp .env.example .env              # TELEGRAM_API_ID / TELEGRAM_API_HASH (+ DRIVE_NAS)
docker compose run --rm aggregator    # 1) first login, interactive, once
docker compose up -d aggregator       # 2) silent from the saved session
docker compose logs -f aggregator
```

## Self-healing (always comes back)

The container is `restart: always`, so it returns after any exit, reboot, or
Docker daemon restart. A crash already exits (and restarts); the hard case is a
**hang** -- the process staying alive but wedged (Telethon stops talking, or the
event loop stalls), which no `restart:` policy can catch because nothing exits.
So the app runs an **in-process watchdog**: the status loop stamps a heartbeat
file (`<state>/health`) each minute, but only after a successful Telegram probe
(`get_me`); a daemon thread `os._exit(1)`s if that heartbeat goes stale past
`runtime.watchdog_sec` (default 600s), turning the hang into an exit that
`restart: always` recovers. State is committed per operation, so the abrupt exit
loses nothing. The compose `healthcheck` reads the same file so Container
Manager shows healthy/unhealthy (it does not itself restart -- the watchdog
does). Tune or disable via the `runtime` section of the constants JSON
(`watchdog_sec: 0` turns it off).

## Log in once -- reboots don't ask again

After the first login the auth key is saved in the session **file**; later
starts are silent across reboots and shutdowns because the file persists (on
`/data` in Docker, or in the checkout when run bare).

**Generate the session on another machine (e.g. Windows) and point at it.**
Run `python -m minions.aggregator.login` there once; it writes and locates the
`telethon.session` file. Copy that file to where the aggregator runs -- for
Docker, `<DRIVE_NAS>/bots/aggregator/telethon.session` (the host path behind
`/data`); same base name, so no rename -- **no rebuild**. See
`deploy/windows/README.md`.

> The `.session` file is full account access. It is git-ignored; don't share it,
> and revoke it from **Telegram -> Settings -> Devices** if it leaks.

## Human-like cat reactions (`cats` engine)

The same account **reacts** to people who **comment on the last posts** with a
**premium cat emoji** -- the cat shows as a **reaction pill on the comment
itself**, not a reply in the thread -- **once per (post, commenter)**, timed and
chosen so it reads as a distracted human rather than a scheduler. The logic
lives in `cats.py` (Telethon-free and unit-tested in `tests/test_cats.py`) and
is driven entirely from the `cats` section of `aggregator_constants.json`.

**New posts are reacted to immediately.** As soon as the aggregator posts, it
drops a cat reaction on its own fresh post right away (no human-like wait) --
the comment reactions keep the distracted-human timing.

> Custom-emoji reactions need a **Premium** account (already required for the
> premium emoji in posts) and a chat that **allows custom-emoji reactions**
> (the channel/discussion admins enable them in the chat's Reactions setting).
> If they are off, the send is logged and skipped -- nothing crashes.

A runnable, network-free proof of the whole path (new post reaction -> comment
reactions, with the dedup and timing) is `cats_proof.py`:
`python -m minions.aggregator.cats_proof`.

**Once per (post, commenter):** a person's *second* comment under the *same*
post gets **no** reply; the *same* person commenting under a *different* post
is eligible for a new cat. (Dedup keys for posts that roll out of the
`watch_posts` window are pruned, so the persisted state stays bounded.)

To tune it: `emoji[].id` are real premium **cat**-emoji ids (add more with the
`/emojis` dump helper), `tz_offset_hours` is the persona's timezone, and
`"enabled": false` turns it off.

How it stays human (the nine principles, all tunable in the JSON):

1. **Timing** is a mixture-of-Gaussians density over the day, separate
   weekday/weekend curves, near-zero in `quiet_hours` -- not `uniform(0,24)`.
2. **Intervals** are heavy-tailed (log-normal), so cats come in bursts then
   long silences -- not a flat cadence.
3. **Selection has memory**: weight = base preference x recency penalty, so
   favourites lead and a just-used cat fades.
4. **Mood** does an AR(1) random walk day to day and tilts sleepy vs. lively.
5. **Context tags** (daypart, season, December) re-weight the pool.
6. **Jitter** takes the fire time off the `:00` second.
7. **Imperfection**: a comment is sometimes ignored (`skip_prob`), a whole day
   is sometimes silent (`silent_day_prob`), and a rare second cat follows.
8. **Feedback**: a reply to the freshest post gets a faster reaction.
9. **State** persists (`cats_state.json` next to the aggregator state): mood,
   the spacing cursor, per-cat recency, who was already catted, **the watched
   posts, and the cats scheduled but not yet sent** -- so a nightly NAS
   shutdown loses nothing.

**Host uptime -- declared *and* learned.** `active_start_hour` /
`active_end_hour` (local hours) are a **prior** -- a starting guess like 7-17.
The bot also **learns the NAS's real on-hours**: a heartbeat records the
current hour while it runs, and the schedule blends the learned curve with the
declared window by confidence (`uptime_learn_obs` heartbeats for full trust,
`uptime_half_life_sec` fades old data). So it adapts to whatever hours the NAS
is actually up -- even outside 7-17 -- and follows a changed schedule on its
own. Set the window to `0`/`24` to lean entirely on learning. A cat that would
land while the host is down is kept in the persisted pending queue and
**re-armed on the next boot** (missed ones renewed to a fresh slot so a night's
worth doesn't fire at once). Watched posts and the learned uptime survive the
restart too.

**Don't double-answer** (`skip_if_manually_replied`, default true): before a
cat fires (which can be hours after the comment), the bot checks whether the
operator has **already answered that comment by hand** -- a manual reply to it,
or a manual reaction already on it -- if so, it skips the cat instead of piling
on.

**Channel vs. group target** (`comments_in_discussion`, default `true`):
- **Channel with a linked discussion** (`true`): each post's comments live in
  the discussion group. The bot resolves the post's **discussion thread** and
  reacts **only to comments on that channel post** -- off-topic discussion
  messages and channel messages are ignored. The account **must be a member of
  the discussion group** to receive those comments.
- **Plain group** (`false`): comments are matched as direct replies to the post
  message id, in the group itself.

**Auto-rescan (per profile).** The bot re-scans the targets on a timer so a post
made (and commented on) while it runs is picked up without a manual `/requeue`.
The cadence differs by profile so you can iterate fast in test but stay quiet in
production: **test = `rescan_sec_test` (5 min)**, **live = `rescan_sec_live`
(1 hour)** in the `cats` JSON (both fall back to `rescan_sec`). `/status` shows a
countdown to the next run.

**Inspect it live** with the `/status` command (from any chat, renders into the
source chat): it lists the videos still **pending** (and which platforms each
awaits), a tail of what was **posted**, the **rejected** (non-Short) count, and
the cat engine's state (enabled, pool size, watched posts, people catted,
pending replies, mood) -- followed by a plain-language legend of the expected
behaviour (`status_help` in the JSON).

## Users database (`users.py`, opt-in)

A per-profile **SQLite** database (`users.db`, next to the other state files)
that records the channel audience over time. **Off by default** -- it collects
personal data. Turn it on in the `users` section of the constants JSON:

```json
"users": { "enabled": true, "store_message_text": true, "enrich": true }
```

What it records:

- **Membership timeline** -- every subscribe/unsubscribe, in order
  (join -> leave -> re-join -> ...), fed from the greeter's admin-log stream.
  So membership needs the same **admin** rights the greeter does; the DB fills
  from the moment you enable it (the admin log only retains a few days).
- **Identity** -- user id, and (via `get_entity`, when `enrich` is on)
  username and first/last name.
- **Messages** -- every comment the account can see in the linked discussion
  group (and the source chat), with the text unless `store_message_text` is
  `false`. Counts, first/last-seen, and the full text are kept per user.

Read it with **`/users`** (totals, top commenters, recent join/leave, rendered
into the source chat); `/status` gains a one-line users summary. The file is a
normal SQLite DB, so you can also query it directly:
`sqlite3 <DRIVE>/bots/aggregator/users.db 'SELECT * FROM membership_events'`.

> **Limits, on purpose.** **Phone is essentially never available** -- Telegram
> exposes `User.phone` only to mutual contacts, so that column is null for
> virtually everyone. Only messages the account **sees** are logged (discussion
> comments and the source chat) -- never DMs or plain channel posts. Every write
> is idempotent, so re-polls and comment rescans never double-count. This is
> **PII**: it lives only on your own state disk, `test` and `live` keep separate
> databases, and nothing is collected while `enabled` is `false`.

## Human-like story viewing (`stories.py`, opt-in)

The account also **watches stories** the way a person idly would -- and only
that: it **never reacts, likes or replies**, it just *views* them and keeps a
log of whose stories it watched. **On by default** -- turn it off any time with
**`/stories_off`**, or in the `stories` section of the constants JSON:

```json
"stories": { "enabled": true, "include_archived": true, "poll_sec_live": 1800 }
```

How it behaves like a person, not a scraper:

- **Only what's new.** It reads Telegram's own active-stories feed -- which
  already returns *contacts / people you follow*, never your whole address book
  -- and views only the stories **past a persisted per-peer seen set**. A story
  watched once is never re-opened, across restarts.
- **Archived contacts** (people whose chats you moved to the Archive) live in a
  separate *hidden* feed. By default they are left alone; set
  `"include_archived": true` to view their stories too -- the hidden feed is
  then polled and merged in, deduped by peer.
- **A glance, not a sweep.** Each poll views a small **session** of peers
  (`per_session_min..max`), **skips some** (`skip_peer_prob`), takes the
  **freshest first**, and staggers the views over lognormal gaps -- then goes
  quiet for a long, heavy-tailed `spacing_*` while before the next session.
- **Quiet hours + the odd silent day**, read in the persona's timezone, so it
  is asleep overnight and occasionally does not show up at all.
- **A human dwell** (`dwell_min..max_sec`) between opening one story and the
  next, so a peer's set is not blinked through instantly.

It shares its human-timing kit (`humanize.py`) with the `cats` engine, so the
"when does a person act" logic lives in one place. The re-poll cadence follows
the profile like the cat rescan does: **test tight (5 min)**, **live relaxed
(30 min)**. Read the log with **`/stories`** (how many, and whose, most recent
first); `/status` gains a one-line stories summary. State is a per-profile
`stories_state.json`; `test` and `live` keep separate seen sets and logs.

## Feature switches (`/features`, on/off at runtime)

Every toggleable feature -- **`cats`**, **`stories`**, **`users`**,
**`greeter`** -- can be turned on or off from chat, without editing the JSON or
restarting the container:

- **`/features`** lists each one and whether it is on.
- **`/<name>_on`** / **`/<name>_off`** flips it, e.g. `/stories_off`,
  `/cats_on`, `/users_off`.

A switch **persists** to `feature_overrides.json` in the base state dir, so the
choice **survives a restart** and overrides that feature's `enabled` default in
the constants JSON. Flipping one restarts the active profile's loops so the
change takes effect immediately; the override is shared by both profiles (like
the single JSON `enabled`). To go back to the JSON default, flip it the other
way (or delete the file).
