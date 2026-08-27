# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Aggregate the same video across platforms, then post the collected links.

A userbot listens to a source chat where bots (or people) drop one JSON object
per video per platform:

    {"platform": "youtube", "caption": "...", "link": "https://...",
     "thumnailUrl": "https://...jpg", "duration": "0:0:16"}

Messages whose captions match ~90% are treated as the same video. Once all the
expected platforms have arrived (or a timeout elapses), one message collecting
every platform's link is posted to the target chat.

Only Shorts are aggregated: a message whose known ``duration`` reaches 3
minutes marks that video as full-length and drops it. Platforms are ranked by
priority (tiktok, youtube, pinterest, instagram): the order sets the link order
in the post and picks which platform's caption leads (the thumbnail is taken
from YouTube only). After a video is posted, its source message ids are saved
to disk; on restart, backfill skips any message whose id was already posted, so
re-posting never happens. A second guard covers a video the source RE-DELIVERS
under new ids (an upstream re-emit -- common once the chat's auto-delete has
cleared the old messages): a title >= ``title_match`` similar to a recent post
is not posted again. "Recent" is two overlapping windows and either one blocks:
a TIME window (posted within ``repost_guard_sec`` seconds, 0 disables) and a
COUNT window (among the last ``repost_guard_count`` posted videos, 0 disables).
The count window is clock-independent, so once a title goes out the next
``repost_guard_count`` distinct videos must post before that title is eligible
again -- it holds even if the state file was just restored with stale
timestamps, or the source floods faster than the time window expects.

Notes:
    * The link is read from ``link`` (or ``url``); the thumbnail from
      ``thumnailUrl`` (the API spelling), then ``thumbnailUrl``/``thumbnail``.
    * ``thumnailUrl`` is optional; when present the post is that photo with
      the links as its caption, otherwise a plain text message.
    * Messages must be valid JSON; anything that does not parse is ignored.

Every behaviour knob is editable in ``aggregator_constants.json`` (a JSON file,
so it may hold non-ASCII text): platforms, title_match, timeout_sec, backfill,
max_duration_sec, the incoming field names, and the post's texts and emoji.
Runtime state is persisted to ``aggregator_state.json`` (human-readable,
indented) and restored on start: ``posted`` (what went out -- title, links,
time -- doubling as the re-post guard), ``pending`` (videos still collecting
platforms) and ``rejected``. A restart within the timeout window loses nothing
and never re-posts.

The env holds only the deploy knobs: credentials (TELEGRAM_API_ID,
TELEGRAM_API_HASH, optional TELEGRAM_PASSWORD), the monitoring chat
SOURCE_CHAT_ID, and the target chat(s) TARGET_CHAT_ID (comma-separated -- list
several chats to post to all of them). The chats live ONLY in the env, the
behaviour ONLY in the JSON -- there is no overlap. The session file and the
state default to <DRIVE>/bots/aggregator/ (DRIVE is the library root: your
Google Drive on Windows, /data in the NAS container) -- so the session is
created and read from the same place on every host. Override with
TELEGRAM_SESSION_FILE / AGGREGATOR_STATE_DIR; with no DRIVE either, they fall
back next to this package.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient
from telethon import events

from minions.userbot.core.client import build_client
from minions.userbot.core.config import CONSTANTS_FILE
from minions.userbot.core.config import FEATURE_OVERRIDES_FILE
from minions.userbot.core.config import MODE_FILE
from minions.userbot.core.config import STATE_FILE
from minions.userbot.core.config import _load_config
from minions.userbot.core.config import _load_constants
from minions.userbot.core.config import _load_runtime
from minions.userbot.core.config import _read_json
from minions.userbot.core.config import _resolve_session_path
from minions.userbot.core.config import _resolve_state_path
from minions.userbot.core.config import apply_persona
from minions.userbot.core.config import load_env
from minions.userbot.core.humanize import Variety
from minions.userbot.core.models import _THUMB_ALIASES
from minions.userbot.core.models import Config
from minions.userbot.core.models import Group
from minions.userbot.core.models import Posted
from minions.userbot.core.runtime import _touch_health
from minions.userbot.core.runtime import _watchdog
from minions.userbot.core.runtime import configure_logging
from minions.userbot.engines import comod
from minions.userbot.engines import greeter
from minions.userbot.engines import reactions
from minions.userbot.engines import stories
from minions.userbot.engines import users
from minions.userbot.engines.premium_emoji import build_premium_message
from minions.userbot.glue.aggregator import _AggregatorMixin
from minions.userbot.glue.commands import _CommandsMixin
from minions.userbot.glue.comod import _ComodMixin
from minions.userbot.glue.profiles import _ProfilesMixin
from minions.userbot.glue.reactions import _ReactionsMixin
from minions.userbot.glue.status import STATUS_WARM_PEERS
from minions.userbot.glue.status import _StatusMixin
from minions.userbot.glue.stories import _StoriesMixin
from minions.userbot.glue.users import _UsersMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from minions.userbot.core import relationship


log = logging.getLogger('userbot')

# How often to log the pending videos and what each still awaits.
STATUS_INTERVAL = 60


class Userbot(
    _AggregatorMixin,
    _ProfilesMixin,
    _StatusMixin,
    _ComodMixin,
    _StoriesMixin,
    _ReactionsMixin,
    _UsersMixin,
    _CommandsMixin,
):
    """The Telethon userbot host: wires the engines and runs the loops.

    A thin assembly -- the aggregation/posting logic lives in
    ``_AggregatorMixin`` (glue/aggregator.py), the live/test profile and
    per-service modes in ``_ProfilesMixin`` (glue/profiles.py), and each other
    engine in its own glue mixin. This class only builds the profile,
    dispatches incoming events, and runs the status/heartbeat loop.
    """

    def __init__(self, client: TelegramClient, config: Config) -> None:
        """Load the constants and wire the aggregator's state."""
        here = Path(__file__)
        self.client = client
        self.config = config
        self.consts = _load_constants(here.with_name(CONSTANTS_FILE))
        self._raw = _read_json(here.with_name(CONSTANTS_FILE))
        apply_persona(self._raw)  # one persona clock shared by all engines
        keys = [*self.consts.fields.values(), *_THUMB_ALIASES]
        self._keys = tuple(dict.fromkeys(keys))
        # Post-decoration picker: keeps the announce line and love/lead/arrow
        # emoji from repeating on consecutive posts (in-memory; cosmetic).
        self._variety = Variety()
        # State is per PROFILE (live vs test): each mode has its OWN channel
        # AND its own state files (reactions, greeter, posted, dedup), so a
        # test run
        # never touches live state and any future stateful feature is isolated
        # for free. The active mode is a marker in the base state dir; live
        # uses that dir (unchanged), test a 'test/' subdir under it.
        base_state = _resolve_state_path(here.with_name(STATE_FILE))
        self._state_base = base_state.parent
        self._mode_path = self._state_base / MODE_FILE
        self._overrides_path = self._state_base / FEATURE_OVERRIDES_FILE
        self._modes = self._load_service_modes()
        self._react_tasks: set[asyncio.Task[None]] = set()
        self._greeter_task: asyncio.Task[None] | None = None
        self._react_rescan_task: asyncio.Task[None] | None = None
        self._react_next_rescan: float = 0.0  # ts of the next auto-rescan
        self._rescan_sec: float = 300.0  # per-profile, set in _build_profile
        self._enrich_tasks: set[asyncio.Task[None]] = set()
        self._story_tasks: set[asyncio.Task[None]] = set()
        self._stories_task: asyncio.Task[None] | None = None
        self._story_next_poll: float = 0.0  # ts of the next stories re-poll
        # Planned-but-not-yet-fired story views, for the /status queue readout.
        self._pending_views: list[stories.StoryView] = []
        # pre-fire thread refresh debounce, keyed by thread root
        self._thread_rescan_at: dict[int, float] = {}
        # ts of the last GetDiscussionMessageRequest, for the flood throttle
        self._last_discussion_ts: float = 0.0
        rt = self._raw.get('runtime')
        rt = rt if isinstance(rt, dict) else {}
        self._probe_timeout = float(rt.get('probe_timeout_sec', 30.0))
        # The liveness probe (get_me) is the only ALWAYS-ON Telegram request;
        # firing it every 60s status tick hammered the server for no reason.
        # Space it to its own gentler cadence -- still well inside the watchdog
        # window -- while the 60s tick keeps doing its LOCAL bookkeeping
        # (uptime learning, pending log) at full resolution.
        self._probe_interval = float(rt.get('probe_interval_sec', 300.0))
        self._last_probe = 0.0
        self._build_profile()



    def _build_profile(self) -> None:
        """(Re)bind every service to ITS OWN mode -- dir, enabled, channel.

        Each service is off/test/live independently (``self._modes``): test
        state lives in ``base/test``, live in ``base``, off builds it inert
        (``enabled=False``, the loop still runs but no-ops). The poster's
        containers and ``live_targets`` follow the aggregator's mode; the
        greeter's channel follows the greeter's. ``start_profile`` then
        hydrates each from its own files.
        """
        self.mode = self._modes['aggregator']
        pdir = self._service_dir('aggregator')
        self.state_path = pdir / STATE_FILE
        self.groups: list[Group] = []
        self.rejected: set[str] = set()
        self.posted: list[Posted] = []
        self.processed_ids: set[int] = set()
        self._react_tasks = set()
        self._react_next_rescan = 0.0
        self._rescan_sec = self._rescan_interval(self._modes['reactions'])
        # A service is enabled when its mode != 'off' (``_feature_enabled``).
        # The params are frozen, so the flag is swapped via replace.
        reaction_params = replace(
            reactions.load_reaction_params(self._raw),
            enabled=self._feature_enabled('reactions'),
        )
        react_dir = self._service_dir('reactions')
        self._migrate_reaction_state(react_dir)
        self.reactions = reactions.ReactionBrain(
            reaction_params,
            react_dir / 'reactions_state.json',
        )
        # Story viewer: watches friends'/contacts' stories the way a person
        # does -- a glance now and then, no reactions -- with its own
        # per-service seen set and view log. Poll cadence follows its mode.
        story_params = replace(
            stories.load_story_params(self._raw, self._modes['stories']),
            enabled=self._feature_enabled('stories'),
        )
        self.stories = stories.StoryBrain(
            story_params, self._service_dir('stories') / 'stories_state.json'
        )
        self._story_next_poll = 0.0
        self._pending_views = []
        # Users DB: its own SQLite file per mode, so live and test audiences
        # never mix. Config lives in the 'users' JSON section.
        ucfg = self._raw.get('users')
        ucfg = ucfg if isinstance(ucfg, dict) else {}
        self._users_enabled = self._feature_enabled('users')
        self._users_store_text = bool(ucfg.get('store_message_text', True))
        self._users_enrich = bool(ucfg.get('enrich', True))
        self.users = users.UserStore(self._service_dir('users') / 'users.db')
        gchannel = self._profile_channel(self._modes['greeter'])
        greeter_params = replace(
            greeter.load_greeter_params(
                self._raw, gchannel, self._modes['greeter']
            ),
            enabled=self._feature_enabled('greeter'),
        )
        self.greeter = greeter.Greeter(
            self.client,
            greeter_params,
            greeter.GreeterIO(
                self._service_dir('greeter') / 'greeter_state.json',
                self._on_membership_event,
            ),
        )
        # The cabinet ("shkaf"): command-only, so it rides the poster's dir.
        self.comod = comod.CabinetRoster(pdir / 'comod.json')
        self._comod = comod.load_comod_params(self._raw)





























    async def handle(self, event: events.NewMessage.Event) -> None:
        """Dispatch one event: a /command, a comment reaction, or aggregation.

        Commands (/emojis, /preview, /status) work from ANY chat and for
        ANYONE and always render into the source chat; the reaction engine
        watches
        replies to our own posts (any chat); aggregation stays source-scoped.
        """
        text = (event.raw_text or '').strip().lower()
        if await self._command(text):
            return
        if await self._unknown_command(event, text):
            return
        self._record_user_message(event)
        if self.reactions.params.enabled:
            self._maybe_react(event)
        if event.chat_id == self.config.source:
            await self.on_message(event.message)

    async def status_report(self) -> None:
        """Post the pending/posted/reaction diagnostics to the source chat."""
        labels = await self._chat_labels()
        await self._send_status(self._status_text(labels))
        log.info('sent status report to %s', self.config.source)

    async def _send_status(self, text: str) -> None:
        """Send an operator report, rendering its premium-emoji markup.

        /status, /requeue and /reactnow embed `<tg-emoji>` tags for the chosen
        reactions/likes and the pool previews; build_premium_message turns them
        into
        custom-emoji entities, so the REAL premium emoji show (a non-premium
        viewer still sees the fallback glyph). Text without tags sends plain.
        """
        message = build_premium_message(text)
        await self.client.send_message(
            self.config.source,
            message.text,
            formatting_entities=message.entities,
            link_preview=False,
        )

    async def _chat_labels(self) -> dict[int, str]:
        """Resolve every chat shown in /status to a readable @name or title."""
        await self._resolve_attach_labels()
        ids = {self.config.source, *self.config.targets}
        if self.config.test_target:
            ids.add(self.config.test_target)
        ids |= {chat for chat, _ in self.reactions.posts}
        ids |= {v.peer_id for v in self._pending_views}  # story-view queue
        return {cid: await self._chat_label(cid) for cid in ids}

    async def _resolve_attach_labels(self) -> None:
        """Cache the shown peers' @names via the shared chat-label helper.

        Both attachment readouts (comment likes and story views) only keep peer
        ids; resolve the ones about to appear in /status (the most recent) to
        @names through the shared resolver and cache them on each ledger, so it
        is a one-time lookup per peer.
        """
        if self.reactions.params.enabled:
            await self._resolve_rows(
                self.reactions.warmth(), self.reactions.remember
            )
        if self.stories.params.enabled:
            await self._resolve_rows(
                self.stories.warmth(), self.stories.remember
            )

    async def _resolve_rows(
        self,
        rows: list[relationship.Warmth],
        remember: Callable[[str, str], None],
    ) -> None:
        """Resolve the shown rows' raw-id labels to @names and cache them."""
        for row in rows[:STATUS_WARM_PEERS]:
            pid = row.label
            if pid.lstrip('-').isdigit():  # still a raw id -> resolve it
                remember(pid, await self._chat_label(int(pid)))

    async def _chat_label(self, chat_id: int) -> str:
        """Return a chat's @username (or "title") for /status, else id."""
        try:
            entity = await self.client.get_entity(chat_id)
        except Exception:  # noqa: BLE001 -- not cached/reachable: show the id
            return str(chat_id)
        username = getattr(entity, 'username', None)
        if username:
            return f'@{username} ({chat_id})'
        title = getattr(entity, 'title', None) or getattr(
            entity, 'first_name', None
        )
        return f'"{title}" ({chat_id})' if title else str(chat_id)

    async def status_loop(self) -> None:
        """Periodically log pending videos, learn uptime, beat the watchdog."""
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            now = time.time()
            await self._maybe_probe(now)
            if self.reactions.params.enabled:
                self.reactions.mark_alive(now)  # learn actual on-hours
            self._log_pending()

    async def _maybe_probe(self, now: float) -> None:
        """Probe Telegram only every _probe_interval, not every 60s tick.

        The liveness check is the sole always-on request, so spacing it stops
        it hammering the server while the stamp still lands well inside the
        watchdog window. The marker advances even on failure, so a wedged link
        is retried gently on the next interval, never in a tight loop.
        """
        if now - self._last_probe >= self._probe_interval:
            self._last_probe = now
            await self._heartbeat()

    def _log_pending(self) -> None:
        """Log each still-collecting group and which platforms it awaits."""
        for group in self.groups:
            missing = [
                p for p in self.config.platforms if p not in group.items
            ]
            log.info(
                'pending %r: have [%s], still waiting for [%s]',
                group.title,
                ', '.join(sorted(group.items)),
                ', '.join(missing),
            )

    async def _heartbeat(self) -> None:
        """Prove end-to-end liveness, then refresh the watchdog heartbeat.

        A cheap Telegram round-trip (``get_me``) under a timeout: on success we
        are both loop-alive AND actually talking to Telegram, so we stamp the
        health file. On a hang/timeout we skip the stamp, letting the file age
        until the watchdog thread restarts the process. Reaching this line at
        all already proves the event loop is not stalled.
        """
        try:
            await asyncio.wait_for(
                self.client.get_me(), timeout=self._probe_timeout
            )
        except Exception:  # noqa: BLE001 -- wedged/unreachable: let it go stale
            log.warning('watchdog: liveness probe failed; heartbeat stale')
            return
        _touch_health()














async def main() -> None:
    """Listen to the source chat and aggregate videos across platforms."""
    configure_logging()
    load_env()

    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        msg = 'Set TELEGRAM_API_ID and TELEGRAM_API_HASH.'
        raise SystemExit(msg)
    config = _load_config()

    session_path = _resolve_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = build_client(session_path, int(api_id), api_hash)
    agg = Userbot(client, config)

    # Listen everywhere the account can see: the /emojis preview command works
    # from ANY chat and for ANYONE (it renders back into the source chat);
    # aggregation itself stays scoped to the source chat inside agg.handle.
    client.add_event_handler(agg.handle, events.NewMessage())

    # TELEGRAM_PASSWORD supplies the 2FA/cloud password non-interactively;
    # unset, Telethon prompts for it (getpass) only if the account has 2FA.
    start_kwargs: dict[str, object] = {}
    password = os.environ.get('TELEGRAM_PASSWORD')
    if password:
        start_kwargs['password'] = password
    log.info('Session store: %s.session', session_path)
    await client.start(**start_kwargs)
    # Hydrate the active profile (live or test) and start its loops. The
    # reaction
    # startup inside can fail without stopping the bot from listening.
    await agg.start_profile()
    log.info(
        'Listening on %s; mode=%s posting to %s; platforms=%s',
        config.source,
        agg.mode,
        ','.join(str(t) for t in agg.live_targets()),
        ','.join(config.platforms),
    )
    status_task = asyncio.create_task(agg.status_loop())
    # Self-healing watchdog: seed the heartbeat now (so a cold start is not
    # instantly "stale"), then a daemon thread exits the process if the
    # heartbeat later goes stale -- a hang no restart: policy could catch --
    # so Docker's restart: always recreates the container.
    _touch_health()
    watchdog_sec = float(_load_runtime().get('watchdog_sec', 600.0))
    threading.Thread(
        target=_watchdog, args=(watchdog_sec,), daemon=True
    ).start()
    await client.run_until_disconnected()
    status_task.cancel()
    await agg.stop_profile()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
