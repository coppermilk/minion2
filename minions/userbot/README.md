# Userbot (Telethon persona)

A **Telethon** userbot -- one real Telegram **user account** over MTProto, **not**
a Bot API bot -- that behaves like a person across several independent engines.
Link aggregation is just one of them:

| Engine | What it does |
|--------|--------------|
| **aggregator** | groups one Short's per-platform links and posts the collected message (with **premium / custom emoji**) to the target chat(s) |
| **reactions** | reacts to / replies to people who comment on the last posts, timed and chosen so it reads as a distracted human |
| **stories** | watches contacts' stories the way a person idly would (view + a small reaction), never a sweep |
| **greeter** | welcome / farewell DMs to channel subscribers |
| **comod** | the supporter "cabinet" (`/comod`): a named shelf per donor for a month |
| **users** | an opt-in SQLite record of the channel audience over time |

`main.py` is only the **host**: it loads config, builds the active profile,
routes each incoming event to the engines, and runs the status/heartbeat loop.
Every engine's behaviour lives in its own module.

## Why a userbot and not a bot

Premium (custom) emoji can only be **sent** by a user account with Telegram
Premium; the Bot API cannot. So this uses Telethon with a **user session**, not a
bot token. `engines/premium_emoji.py` turns Bot-API-style `<tg-emoji
emoji-id="...">` markup into Telethon `MessageEntityCustomEmoji` entities
(measured in UTF-16 code units, as Telegram requires).

## Layout

```
minions/userbot/
  main.py                 # the Userbot host + entry point (python -m minions.userbot.main)
  login.py                # one-time interactive login -> the session file
  aggregator_constants.json, assets/   # editable texts + premium-emoji ids (UTF-8), cabinet template/fonts
  core/                   # pure building blocks, no Telethon
    models, matching, state (the store), statefile, render, config, codec,
    runtime, client, humanize, tasks, attachment, relationship
  engines/                # domain brains (Telethon-free, unit-tested)
    reactions, stories, greeter, comod, users, premium_emoji
  glue/                   # the collaborators that DO touch Telethon
    aggregator (LinkAggregator), reactions (CommentWatch),
    stories (StoryWatch), comod (Cabinet), users (AudienceLog),
    status (StatusReport), commands (CommandRouter), profiles (ServiceModes)
  dev/                    # developer tools, not the running bot
    dump_emoji_ids
```

The principle: `core/` is pure logic/maths/IO, `engines/` are the domain brains,
`glue/` is everything that calls Telethon, `dev/` is helpers. The aggregation
brain (grouping, fuzzy title match, the re-post guard) lives in `core/matching.py`
+ `core/models.py`; its Telethon side is `glue/aggregator.py`.

**How the host is assembled.** `Userbot` is a plain object that HOLDS one
collaborator per service -- it does not inherit from them. Each service takes a
frozen `*Deps` naming everything it may reach, so what a service can touch is
its constructor signature, not "whatever is on `self`". The three host-level
helpers (`StatusReport`, `CommandRouter`, `ServiceModes`) hold the bot
explicitly instead. Two consequences worth knowing: a service cannot reach
another service's state by accident, and every collaborator can be constructed
in a test without a Telethon client.

## Configuration

Two sources, no overlap: the **env** carries only deploy knobs (credentials and
the chats); **`aggregator_constants.json`** carries all behaviour and content.

**env**

| Env | Meaning |
|-----|---------|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | credentials (from <https://my.telegram.org>) |
| `TELEGRAM_PASSWORD` | optional 2FA/cloud password |
| `SOURCE_CHAT_ID` | the chat the per-platform JSON arrives in (monitoring) |
| `TARGET_CHAT_ID` | target chat(s) -- **comma-separated** to post to several |
| `TELEGRAM_SESSION_FILE` | session-file base path override (`.session` appended) |
| `AGGREGATOR_STATE_DIR` | state-dir override |
| `DRIVE` | library root; session + state default to `<DRIVE>/bots/aggregator/` |

> The on-disk data dir keeps its historical name `bots/aggregator/` (and the
> `AGGREGATOR_STATE_DIR` env) so the live bot finds its session and state exactly
> where it always did -- only the **code** is now `minions.userbot`.

**`aggregator_constants.json`** -- six sections, one rule: **a setting lives
in the section of the engine that reads it.**

| Section | What is in it |
|---------|---------------|
| `persona` | one human: timezone, waking window, silent-day chance. Fanned into every engine at load, so you tune the person in one place |
| `runtime` | the process: watchdog, liveness probe, flood-sleep threshold |
| `engines` | one block per engine -- `aggregator` (platforms, timeouts, the dedup/flood guards, the incoming `fields` names), `reactions`, `stories`, `greeter`, `comod`, `users` |
| `post` | what a post looks like: author, announce lines, rows, labels, samples |
| `emoji` | the unified premium-emoji catalog, each entry tagged with its `type` |
| `texts` | what the operator sees: `/help`, the `/status` legend and glyphs, the words that suppress a sticker |

## State on disk

Two shapes, because two things behave differently. Per-peer data grows with
the audience and is written one row at a time; cursors are a bounded handful
of scalars and are the only part worth reading by eye.

```
<DRIVE>/bots/aggregator/       (live; test/ mirrors it under the same dir)
  telethon.session             the MTProto session (SQLite, WAL)
  peers.db                     per-peer ledger + dedup marks, both engines
  cursors.json                 mood, session marks, daily counters, queues
  aggregator_state.json        the posted log + groups still collecting
  greeter_state.json           the admin-log cursor and DM budget
  comod.json                   who is on which shelf
  users.db                     the opt-in audience DB (PII, off by default)
```

`peers.db` is the source of truth; `cursors.json` is rebuilt from what the
store holds rather than written per event -- at a thousand peers the old
single-file design reached 652 KB and 47 000 lines and was rewritten on
every comment. `users.db` stays separate on purpose: it is opt-in and holds
PII, which the always-on ledger does not.

## Run (without Docker)

```bash
pip install -e '.[tg]'            # from the repo root
cp .env.example .env              # fill in TELEGRAM_API_ID / TELEGRAM_API_HASH
python -m minions.userbot.login   # once, interactive -- writes the session file
python -m minions.userbot.main    # silent from the saved session, every run after
```

## Run in Docker (this project's shared image)

The userbot rides the **one shared image** (`telethon` is baked in via the `tg`
extra). Compose (the `userbot` service) mounts `${DRIVE_NAS}:/data`, sets
`TELEGRAM_SESSION_FILE=/data/bots/aggregator/telethon`, and reads `.env`.

```bash
docker compose run --rm userbot     # 1) first login, interactive, once
docker compose up -d userbot        # 2) silent from the saved session
docker compose logs -f userbot
```

## Self-healing + gentle on Telegram

`restart: always` brings the container back after any exit. A **hang** (process
alive but wedged) is caught by an in-process **watchdog**: the status loop stamps
a heartbeat file only after a successful Telegram probe (`get_me`); a daemon
thread `os._exit(1)`s if that heartbeat goes stale past `runtime.watchdog_sec`
(default 600s), turning the hang into an exit the policy recovers.

The client is built to respect Telegram rather than hammer it (`core/client.py`):

- **`runtime.flood_sleep_threshold_sec`** (default 3600) -- on a FloodWait the
  client patiently sleeps it off and retries instead of erroring, the single most
  account-friendly behaviour under sustained automation.
- **`runtime.probe_interval_sec`** (default 300) -- the always-on liveness probe
  fires every 5 min, not every 60s status tick, so it stops adding constant
  background load.
- The SQLite **session runs in WAL mode**, so the watchdog's hard exit can no
  longer corrupt the `.session` file (the crash that forced a re-login every
  couple of weeks).

## Reactions engine (was "cats")

The account **reacts** to people who **comment on the last posts** with a chosen
premium emoji -- the glyph shows as a **reaction pill on the comment itself**,
**once per (post, commenter)** -- timed and chosen so it reads as a distracted
human. The engine is Telethon-free (`engines/reactions.py`, unit-tested in
`tests/test_reactions.py`) and driven from the `reactions` section of the JSON.
The reaction glyphs are simply the `type: "reaction"` entries of the unified
`emoji` array -- put cats there, or daisies; the code is content-neutral.

**Persona label (optional).** The commands are neutral: `/reactnow` fires the
queue now, `/reactions_on` / `/reactions_off` toggle the engine. Set
`reactions.label` (e.g. `"cat"`) and the engine ALSO answers under a friendly
name -- `/catnow`, `/cat_on|off|test|live` -- so a persona keeps its own
vocabulary without hard-coding it. Empty (the default) = neutral commands only.

> Custom-emoji reactions need a **Premium** account and a chat that **allows
> custom-emoji reactions**. If off, the send is logged and skipped -- nothing
> crashes.

## Human-like story viewing (`stories`, opt-in)

Reads Telegram's own active-stories feed (contacts / followed peers), views only
**unseen** stories past a persisted per-peer seen set, in small sessions with
human dwell/gaps, quiet hours and the odd silent day. It shares the timing kit
(`core/humanize.py`) with the reactions engine. Read the log with `/stories`;
turn it off with `/stories_off`.

## Users database (`users`, opt-in, off by default)

A per-profile **SQLite** DB (`users.db`) recording the channel audience over time
(membership timeline, identity, seen messages). It collects PII, so it is off by
default and lives only on your own state disk. Read it with `/users`.

## Reading `/status`

Two sections answer the questions that used to need the log.

**Stories** lists everyone who has stories up right now -- the same people you
see in Telegram, archived feed included -- and what the bot decided about each:
`viewing 3 in ~3m 10s`, `passed this glance`, `nothing new`, or the reason the
whole session is held (quiet hours, cooldown, silent day). It is a snapshot from
the last poll with its age beside it (`glance 4m 10s ago`), on purpose: the
report never re-reads the feed, because two story requests behind every `/status`
is exactly the traffic the request gate exists to prevent.

An archived peer is watched on the same maths as anyone else; the `archived feed`
marker says where we saw them, not that they are treated differently.

**Schedule** collects every background loop's next run -- host tick, liveness
probe, reactions rescan, stories poll, greeter check -- plus the request gate:
when each lane may fire next, and how far a lane has been widened after a
FloodWait. That widening is the only place Telegram's "slow down" is visible
without reading the log.

## Commands

From **any** chat, rendered back into the source chat: `/help` (or `/start`),
`/status`, `/preview`, `/emojis`, `/reactnow`, `/requeue`, `/greetnow`,
`/stories`, `/users`, `/comod ...`, `/propiska_shkaf_month`, `/test` / `/live`
(switch the whole bot's profile), `/features`, and per-service toggles
`/<name>_on|off|test|live` (`name` in `aggregator, reactions, stories, users,
greeter`). A toggle **persists** to `aggregator_mode.json` and takes effect at
once.
