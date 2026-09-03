# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The Telethon userbot host: wire the engines, dispatch events, run the loops.

One Telegram USER account behaving like a person across several engines:

    * aggregator -- groups one Short's per-platform links and posts the
      collected message (``glue/aggregator.py`` + the pure logic in
      ``core/matching.py``);
    * reactions -- reacts to/replies to commenters
      (``engines/reactions.py``, ``glue/reactions.py``);
    * stories, greeter, comod, users -- story viewing, welcome DMs, the
      supporter cabinet, the audience DB.

This module is only the HOST. ``__init__`` loads the constants and builds the
active profile; ``_build_profile`` wires every engine to its own live/test
mode (``glue/profiles.py``); ``handle`` routes each incoming event to the
command dispatcher, the reaction engine, and the aggregator; and the status
loop runs the /status log and the self-healing watchdog heartbeat. Each
engine's own behaviour lives in its module, not here.

Configuration is two sources with no overlap. The ENV carries only deploy
knobs -- credentials (TELEGRAM_API_ID/HASH, optional TELEGRAM_PASSWORD) and the
chats (SOURCE_CHAT_ID, comma-separated TARGET_CHAT_ID). Everything BEHAVIOURAL
lives in ``aggregator_constants.json`` (non-ASCII, so texts and emoji fit).
Runtime state persists per profile (``aggregator_state.json`` and the
per-engine ``*_state.json``) and is restored on start, so a restart loses
nothing. The session file and state default to ``<DRIVE>/bots/aggregator/``
(DRIVE is the library root: Google Drive on Windows, ``/data`` in the NAS
container); override with TELEGRAM_SESSION_FILE / AGGREGATOR_STATE_DIR.

Entry point: ``python -m minions.userbot.main``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

from minion_core.adapters import userchat
from minions.userbot.core import codec
from minions.userbot.core import render
from minions.userbot.core import state
from minions.userbot.core.config import CONSTANTS_FILE
from minions.userbot.core.config import apply_persona
from minions.userbot.core.config import load_config
from minions.userbot.core.config import load_constants
from minions.userbot.core.config import load_env
from minions.userbot.core.config import load_runtime
from minions.userbot.core.config import read_json
from minions.userbot.core.config import resolve_session_path
from minions.userbot.core.config import resolve_state_dir
from minions.userbot.core.humanize import Variety
from minions.userbot.core.models import THUMB_ALIASES
from minions.userbot.core.models import Config
from minions.userbot.core.render import Glyphs
from minions.userbot.core.runtime import configure_logging
from minions.userbot.core.runtime import touch_health
from minions.userbot.core.runtime import watchdog
from minions.userbot.core.state import Database
from minions.userbot.core.state import StateStore
from minions.userbot.engines import comod
from minions.userbot.engines import greeter
from minions.userbot.engines import reactions
from minions.userbot.engines import stories
from minions.userbot.engines import users
from minions.userbot.engines.premium_emoji import PremiumMessage
from minions.userbot.engines.premium_emoji import build_premium_message
from minions.userbot.glue.aggregator import AggregatorDeps
from minions.userbot.glue.aggregator import LinkAggregator
from minions.userbot.glue.commands import CommandRouter
from minions.userbot.glue.comod import Cabinet
from minions.userbot.glue.comod import CabinetDeps
from minions.userbot.glue.profiles import ServiceModes
from minions.userbot.glue.reactions import CommentDeps
from minions.userbot.glue.reactions import CommentWatch
from minions.userbot.glue.status import STATUS_WARM_PEERS
from minions.userbot.glue.status import StatusReport
from minions.userbot.glue.stories import StoryDeps
from minions.userbot.glue.stories import StoryWatch
from minions.userbot.glue.users import AudienceDeps
from minions.userbot.glue.users import AudienceLog

log = logging.getLogger('userbot')

# How often to log the pending videos and what each still awaits.
STATUS_INTERVAL = 60


class Userbot:
    """The Telethon userbot host: wires the services and runs the loops.

    A thin assembly. Every behaviour lives in an object this class holds --
    ``aggregator`` posts, ``comment_watch`` reacts, ``story_watch`` views,
    ``greeter`` DMs, ``audience`` records, ``cabinet`` runs /comod, ``modes``
    owns the live/test lifecycle and ``report`` renders /status. The host
    itself only builds them, routes each incoming event, and runs the
    status/heartbeat loop.
    """

    def __init__(self, account: userchat.Account, config: Config) -> None:
        """Load the constants and assemble every service."""
        here = Path(__file__)
        self.config = config
        self.consts = load_constants(here.with_name(CONSTANTS_FILE))
        self.settings = read_json(here.with_name(CONSTANTS_FILE))
        apply_persona(self.settings)  # one persona clock, shared by all
        keys = [*self.consts.fields.values(), *THUMB_ALIASES]
        self._field_keys = tuple(dict.fromkeys(keys))
        # Post-decoration picker: keeps the announce line and love/lead/arrow
        # emoji from repeating on consecutive posts (in-memory; cosmetic).
        self._variety = Variety()
        # State is per PROFILE (live vs test): each mode has its OWN channel
        # AND its own state files (reactions, greeter, posted, dedup), so a
        # test run
        # never touches live state and any future stateful feature is isolated
        # for free. The active mode is a marker in the base state dir; live
        # uses that dir (unchanged), test a 'test/' subdir under it.
        self.modes = ServiceModes(self, resolve_state_dir(here.parent))
        self.report = StatusReport(self)
        self.router = CommandRouter(self)
        self._dbs: dict[Path, Database] = {}
        self.greeter_task: asyncio.Task[None] | None = None
        self.rescan_task: asyncio.Task[None] | None = None
        self.stories_task: asyncio.Task[None] | None = None
        rt = codec.section(self.settings, 'runtime')
        # One door to Telegram, one gate in front of it: every service this
        # host builds is handed the same account, so there is no second way
        # out and nothing that skips the pacing.
        self.account = account
        self._probe_timeout = codec.num(rt.get('probe_timeout_sec'), 30.0)
        # The liveness probe (get_me) is the only ALWAYS-ON Telegram request;
        # firing it every 60s status tick hammered the server for no reason.
        # Space it to its own gentler cadence -- still well inside the watchdog
        # window -- while the 60s tick keeps doing its LOCAL bookkeeping
        # (uptime learning, pending log) at full resolution.
        self._probe_interval = codec.num(rt.get('probe_interval_sec'), 300.0)
        self._last_probe = 0.0
        # The one loop that did not publish its next run; /status shows it
        # beside the others so a stalled tick is visible, not inferred.
        self.next_tick = 0.0
        self.build_profile()

    def _db(self, where: Path) -> Database:
        """Return one profile directory's state database, opening it once.

        Keyed by DIRECTORY, because a service's directory follows its own
        mode: flipping stories to test must open the test file, while the
        live one stays open for whoever is still live. Older state files are
        folded in the first time a directory is opened.
        """
        db = self._dbs.get(where)
        if db is None:
            state.adopt(where)  # fold any older shape in, once
            db = Database(where / state.DB_NAME)
            self._dbs[where] = db
        return db

    def database(self, service: str) -> Database:
        """Return the state database of one service's profile directory.

        Public because identity is not per-service: /who reads ``actors``,
        which every service in that directory shares, so it needs the file
        rather than one service's view of it.
        """
        return self._db(self.modes.service_dir(service))

    def _store(self, service: str, where: Path | None = None) -> StateStore:
        """Return one service's view of its profile's state database.

        The view knows its own name, so nothing downstream passes it -- an
        engine reads and writes as though it owned the file.
        """
        pdir = self.modes.service_dir(service) if where is None else where
        return self._db(pdir).store(service)

    def build_profile(self) -> None:
        """(Re)bind every service to ITS OWN mode -- dir, enabled, channel.

        Each service is off/test/live independently (``self.modes``): test
        state lives in ``base/test``, live in ``base``, off builds it inert
        (``enabled=False``, the loop still runs but no-ops). The poster's
        containers and ``live_targets`` follow the aggregator's mode; the
        greeter's channel follows the greeter's. ``start_profile`` then
        hydrates each from its own files.
        """
        self.mode = self.modes.mode_of('aggregator')
        pdir = self.modes.service_dir('aggregator')
        # A service is enabled when its mode is not 'off'.
        # The params are frozen, so the flag is swapped via replace.
        reaction_params = replace(
            reactions.load_reaction_params(self.settings),
            enabled=self.modes.enabled('reactions'),
        )
        self.reactions = reactions.ReactionBrain(
            reaction_params, self._store('reactions')
        )
        self.comment_watch = CommentWatch(
            CommentDeps(
                account=self.account,
                brain=self.reactions,
                targets=self.live_targets,
                announce=self._send_status,
                glyphs=Glyphs(self.report.bullet(), self.report.arrow()),
                human_words=self.consts.human_words,
                rescan_sec=self.modes.rescan_interval(
                    self.modes.mode_of('reactions')
                ),
            )
        )
        # Story viewer: watches friends'/contacts' stories the way a person
        # does -- a glance now and then, no reactions -- with its own
        # per-service seen set and view log. Poll cadence follows its mode.
        story_params = replace(
            stories.load_story_params(
                self.settings, self.modes.mode_of('stories')
            ),
            enabled=self.modes.enabled('stories'),
        )
        self.stories = stories.StoryBrain(story_params, self._store('stories'))
        self.story_watch = StoryWatch(
            StoryDeps(
                account=self.account,
                brain=self.stories,
                source=self.config.source,
                learn=self._learn,
                name=self._name,
            )
        )
        # Audience log: its own SQLite file per mode, so live and test
        # audiences never mix. Config lives in the 'users' JSON section.
        ucfg = codec.engine(self.settings, 'users')
        self.audience = AudienceLog(
            AudienceDeps(
                account=self.account,
                source=self.config.source,
                store=users.UserStore(
                    self._db(self.modes.service_dir('users')).conn
                ),
                watched=lambda: {c for c, _ in self.reactions.posts},
                enabled=self.modes.enabled('users'),
                store_text=bool(ucfg.get('store_message_text', True)),
                enrich=bool(ucfg.get('enrich', True)),
            )
        )
        gchannel = self.modes.channel_for(self.modes.mode_of('greeter'))
        greeter_params = replace(
            greeter.load_greeter_params(
                self.settings, gchannel, self.modes.mode_of('greeter')
            ),
            enabled=self.modes.enabled('greeter'),
        )
        self.greeter = greeter.Greeter(
            self.account,
            greeter_params,
            greeter.GreeterIO(
                self._store('greeter'),
                self.audience.note_membership,
            ),
        )
        self.aggregator = LinkAggregator(
            AggregatorDeps(
                account=self.account,
                config=self.config,
                consts=self.consts,
                store=self._store('aggregator', pdir),
                targets=self.live_targets,
                on_posted=self.comment_watch.on_posted,
                field_keys=self._field_keys,
                variety=self._variety,
                mode=self.mode,
            )
        )
        # The cabinet ("shkaf"): command-only, so it rides the poster's dir.
        self.cabinet = Cabinet(
            CabinetDeps(
                account=self.account,
                chat=self.config.source,
                roster=comod.CabinetRoster(self._store('comod', pdir)),
                params=comod.load_comod_params(self.settings),
                work_dir=pdir,
            )
        )

    async def handle(self, msg: userchat.Msg) -> None:
        """Dispatch one message: a /command, a comment, or aggregation.

        Commands (/emojis, /preview, /status) work from ANY chat and for
        ANYONE and always render into the source chat; the reaction engine
        watches
        replies to our own posts (any chat); aggregation stays source-scoped.
        """
        text = msg.text.strip().lower()
        if await self.router.handle(text):
            return
        if await self.router.nudge_unknown(msg, text):
            return
        self.audience.record_message(msg)
        if self.reactions.params.enabled:
            self.comment_watch.on_message(msg)
        if msg.chat_id == self.config.source:
            await self.aggregator.on_message(msg)

    async def status_report(self) -> None:
        """Post the pending/posted/reaction diagnostics to the source chat."""
        await self._send_status(self.report.text(await self.peer_actors()))
        log.info('sent status report to %s', self.config.source)

    async def _send_status(self, text: str) -> None:
        """Send an operator report, rendering its premium-emoji markup.

        /status, /requeue and /reactnow embed `<tg-emoji>` tags for the
        chosen reactions/likes and the pool previews; build_premium_message
        turns them into custom-emoji spans, so the REAL premium emoji show
        (a non-premium viewer still sees the fallback glyph). Text without
        tags sends plain.
        """
        await self.show(build_premium_message(text))

    async def show(self, message: PremiumMessage) -> None:
        """Send an already-composed premium message to the operator."""
        await self.account.send(
            self.config.source,
            userchat.Text(message.text, message.spans),
        )

    async def say(self, text: str) -> None:
        """Send a plain operator line to the source chat."""
        await self.account.send(self.config.source, userchat.Text(text))

    async def peer_actors(self) -> dict[int, state.Actor]:
        """Return who everyone /status names is, resolving what we lack.

        The report is handed FIELDS, not display strings, and composes what
        each of its sections wants (``core/render.name`` for a list of
        people, ``tagged`` for the routing lines, which are read to
        configure chats and want the id). A single string could not serve
        both, which is why the id used to be baked in at resolve time and
        stripped back off at render time.
        """
        chats = {self.config.source, *self.config.targets}
        if self.config.test_target:
            chats.add(self.config.test_target)
        chats |= {chat for chat, _ in self.reactions.posts}
        chats |= {v.peer_id for v in self.story_watch.pending}
        for chat_id in chats:
            await self._learn(chat_id)
        people = await self._warm_people()
        return self._actors([*chats, *people])

    async def _warm_people(self) -> list[int]:
        """Resolve a FEW of the people /status is about to name; return all.

        The glance and the two warmth readouts list people we may never
        have opened, and resolving every one of them each minute is exactly
        the burst the request gate exists to prevent. A bounded handful per
        report, and a few reports in the whole list is known and costs
        nothing again.
        """
        shown: list[int] = []
        if self.reactions.params.enabled:
            shown += [w.peer_id for w in self.reactions.warmth()]
        if self.stories.params.enabled:
            shown += [w.peer_id for w in self.stories.warmth()]
            shown += [r.peer_id for r in self.stories.last_glance.peers]
        shown = list(dict.fromkeys(shown))
        known = self._actors(shown)
        for peer_id in [p for p in shown if p not in known][
            :STATUS_WARM_PEERS
        ]:
            await self._learn(peer_id)
        return shown

    def _actors(self, peer_ids: list[int]) -> dict[int, state.Actor]:
        """Return what every open profile database knows about these peers.

        Merged across the open databases because a service in test keeps its
        own file: the same person may be known in one and not the other, and
        who they are does not depend on which profile met them first.
        """
        found: dict[int, state.Actor] = {}
        for db in self._dbs.values():
            found.update(db.actors(peer_ids))
        return found

    async def _learn(self, chat_id: int) -> str:
        """Resolve a chat through Telegram, record WHO it is, and name it.

        Returns the readable name (empty when the peer cannot be reached).
        Writes to every open profile database: identity is the same fact
        wherever it is read, and each profile keeps its own copy of it.
        """
        peer = await self.account.peer(chat_id)
        if peer is None:
            return ''
        actor = state.Actor(
            peer_id=chat_id,
            kind='chat' if chat_id < 0 else 'user',
            username=peer.username,
            title=peer.title,
            first_name=peer.first_name,
            last_name=peer.last_name,
            phone=peer.phone,
        )
        for db in self._dbs.values():
            db.note_actor(actor)
        return render.name(actor)

    def _name(self, peer_id: int) -> str:
        """Return what we already know a peer as, without asking Telegram."""
        found = self._actors([peer_id]).get(peer_id)
        return render.name(found) if found is not None else ''

    async def status_loop(self) -> None:
        """Periodically log pending videos, learn uptime, beat the watchdog."""
        while True:
            self.next_tick = time.time() + STATUS_INTERVAL
            await asyncio.sleep(STATUS_INTERVAL)
            now = time.time()
            await self._maybe_probe(now)
            if self.reactions.params.enabled:
                self.reactions.mark_alive(now)  # learn actual on-hours
            self._log_pending()

    def next_probe(self) -> float:
        """Return when the liveness probe fires next (0 before the first)."""
        if not self._last_probe:
            return 0.0
        return self._last_probe + self._probe_interval

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

    def live_targets(self) -> tuple[int, ...]:
        """Post destination for the active profile.

        Test: TEST_CHAT_ID, or the source control chat if it is unset. Live:
        the configured targets. Every channel-touching part reads this, so
        the whole bot follows the profile.
        """
        if self.mode == 'test':
            return (self.config.test_target or self.config.source,)
        return self.config.targets

    def _log_pending(self) -> None:
        """Log each still-collecting group and which platforms it awaits."""
        for group in self.aggregator.groups:
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
            alive = await asyncio.wait_for(
                self.account.me(), timeout=self._probe_timeout
            )
        except TimeoutError:
            alive = None
        if alive is None:
            log.warning('watchdog: liveness probe failed; heartbeat stale')
            return
        touch_health()


def _login(session: Path, api_id: str, api_hash: str) -> userchat.Login:
    """Build the session credentials, flood knob included."""
    rt = load_runtime()
    threshold = codec.num(
        rt.get('flood_sleep_threshold_sec'), userchat.DEFAULT_FLOOD_SLEEP
    )
    return userchat.Login(session, int(api_id), api_hash, threshold)


async def main() -> None:
    """Listen to the source chat and aggregate videos across platforms."""
    configure_logging()
    load_env()

    api_id = os.environ.get('TELEGRAM_API_ID')
    api_hash = os.environ.get('TELEGRAM_API_HASH')
    if not api_id or not api_hash:
        msg = 'Set TELEGRAM_API_ID and TELEGRAM_API_HASH.'
        raise SystemExit(msg)
    config = load_config()

    session_path = resolve_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = userchat.connect(_login(session_path, api_id, api_hash))
    account = userchat.Account(client, userchat.paces(load_runtime()))
    bot = Userbot(account, config)

    # Listen everywhere the account can see: the /emojis preview command works
    # from ANY chat and for ANYONE (it renders back into the source chat);
    # aggregation itself stays scoped to the source chat inside bot.handle.
    bot.account.on_message(bot.handle)

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
    await bot.modes.start_profile()
    log.info(
        'Listening on %s; mode=%s posting to %s; platforms=%s',
        config.source,
        bot.mode,
        ','.join(str(t) for t in bot.live_targets()),
        ','.join(config.platforms),
    )
    status_task = asyncio.create_task(bot.status_loop())
    # Self-healing watchdog: seed the heartbeat now (so a cold start is not
    # instantly "stale"), then a daemon thread exits the process if the
    # heartbeat later goes stale -- a hang no restart: policy could catch --
    # so Docker's restart: always recreates the container.
    touch_health()
    watchdog_sec = codec.num(load_runtime().get('watchdog_sec'), 600.0)
    threading.Thread(
        target=watchdog, args=(watchdog_sec,), daemon=True
    ).start()
    await client.run_until_disconnected()
    status_task.cancel()
    await bot.modes.stop_profile()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopped.')
