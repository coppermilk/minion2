# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Each service's off/test/live mode, and the profile lifecycle around it.

Every service runs in its own mode, with its own state directory: test state
lives under ``base/test``, live (and off) in ``base``, so one service can be
sandboxed while the others stay live. This object owns that map, persists it,
and drives the teardown/rebuild/hydrate cycle a mode change needs.

It holds the bot rather than sharing its ``self``, so what it reaches --
which services to stop, which to start -- is visible at every call site.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from minions.userbot.core import codec
from minions.userbot.core.config import MODE_FILE
from minions.userbot.core.runtime import cancel
from minions.userbot.glue.commands import SERVICE_MODES
from minions.userbot.glue.commands import SERVICE_NAMES

if TYPE_CHECKING:
    from pathlib import Path

    from minions.userbot.main import Userbot

log = logging.getLogger('userbot')


class ServiceModes:
    """The per-service mode map, its file, and the profile lifecycle."""

    def __init__(self, bot: Userbot, base: Path) -> None:
        """Bind the bot and the base state dir, then load the stored modes."""
        self.bot = bot
        self.base = base
        self.path = base / MODE_FILE
        self.by_service = self._load()

    def mode_of(self, name: str) -> str:
        """Return one service's mode ('off' when it is not recorded)."""
        return self.by_service.get(name, 'off')

    def enabled(self, name: str) -> bool:
        """Whether a service is active (its mode is not 'off')."""
        return self.mode_of(name) != 'off'

    def service_dir(self, name: str) -> Path:
        """Return (and create) the state dir for a service's OWN mode."""
        pdir = self._profile_dir(self.by_service[name])
        pdir.mkdir(parents=True, exist_ok=True)
        return pdir

    def rescan_interval(self, mode: str) -> float:
        """Return the auto-rescan period for MODE: test fast, live slow.

        Test wants a tight loop while you iterate (default 5 min); live can be
        relaxed (default 1 hour). Both fall back to ``rescan_sec``.
        """
        cfg = codec.engine(self.bot.settings, 'reactions')
        default = codec.num(cfg.get('rescan_sec'), 300.0)
        key = 'rescan_sec_test' if mode == 'test' else 'rescan_sec_live'
        return codec.num(cfg.get(key), default)

    def channel_for(self, mode: str) -> int:
        """Return the greeter's default channel for MODE (test = test chat)."""
        config = self.bot.config
        if mode == 'test':
            return config.test_target or config.source
        return config.targets[0] if config.targets else 0

    def save(self) -> None:
        """Persist every service's mode so a restart resumes them."""
        self.path.write_text(
            json.dumps({'services': self.by_service}, indent=2),
            encoding='utf-8',
        )

    async def set(self, name: str, mode: str) -> None:
        """Set one service's mode, persist it, and restart its loops.

        No-op-reports when already there; otherwise records the mode, tears
        the profile down and rebuilds it so this service's dir, enabled flag
        and destination take effect while the others keep their own modes.
        """
        if self.mode_of(name) == mode:
            await self.bot.say(f'{name}: already {mode}')
            return
        self.by_service[name] = mode
        self.save()
        await self.stop_profile()
        self.bot.build_profile()
        await self.start_profile(source_backfill=False)
        log.info('service %s -> %s', name, mode)
        await self.bot.say(f'{name}: {mode}')

    async def switch_all(self, mode: str) -> None:
        """Switch every ACTIVE service to MODE (the /test and /live commands).

        A total sandbox: the poster always follows, and any service currently
        on moves to MODE too; a service that is off stays off. Tear the
        profile down, rebind each service, hydrate. Persisted, so a restart
        comes up the same. Per-service overrides use ``set``.
        """
        self.by_service = {
            n: mode if (n == 'aggregator' or m != 'off') else 'off'
            for n, m in self.by_service.items()
        }
        await self.stop_profile()
        self.bot.build_profile()
        self.save()
        await self.start_profile(source_backfill=False)
        labels = await self.bot.chat_labels()
        targets = self.bot.live_targets()
        dest = ', '.join(labels.get(t, str(t)) for t in targets)
        await self.bot.say(
            f'Mode: {self.bot.mode.upper()}. ALL posts now go to: {dest}'
        )
        log.info('mode -> %s, posting to %s', self.bot.mode, targets)

    async def start_profile(self, *, source_backfill: bool = True) -> None:
        """Hydrate the active profile and start its background loops.

        ``source_backfill`` re-scans the source for missed videos -- wanted at
        boot, but skipped on a live<->test switch so entering test never dumps
        a burst of recent videos into the test channel.
        """
        bot = self.bot
        bot.aggregator.restore()
        try:
            bot.comment_watch.rearm()
            await bot.comment_watch.seed_posts()
            await bot.comment_watch.seed_comments()
            if bot.reactions.params.enabled:
                bot.reactions.mark_alive(time.time())
        except Exception:
            log.exception('reactions: startup step failed; listening anyway')
        if source_backfill:
            await bot.aggregator.backfill()
        bot.greeter_task = asyncio.create_task(bot.greeter.loop())
        bot.rescan_task = asyncio.create_task(bot.comment_watch.rescan_loop())
        bot.stories_task = asyncio.create_task(bot.story_watch.loop())

    async def stop_profile(self) -> None:
        """Cancel the active profile's timers and loops (before a switch)."""
        bot = self.bot
        bot.comment_watch.cancel()
        bot.story_watch.cancel()
        bot.audience.close()
        bot.aggregator.cancel()
        for task in (bot.greeter_task, bot.rescan_task, bot.stories_task):
            cancel(task)
        bot.greeter_task = None
        bot.rescan_task = None
        bot.stories_task = None

    def _profile_dir(self, mode: str) -> Path:
        """Return the state dir for MODE: base for live, base/test for test."""
        return self.base / 'test' if mode == 'test' else self.base

    def _load(self) -> dict[str, str]:
        """Load each service's mode from ``{"services": {name: mode}}``.

        A missing file, an unreadable one or a missing block (a fresh
        install) leaves every service on its own default.
        """
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            raw = {}
        stored = raw.get('services')
        stored = stored if isinstance(stored, dict) else {}
        return {n: self._clean(n, stored.get(n)) for n in SERVICE_NAMES}

    def _clean(self, name: str, value: object) -> str:
        """Return a valid stored mode, else the service's default."""
        if value in SERVICE_MODES:
            return str(value)
        return self._default(name)

    def _default(self, name: str) -> str:
        """Return a service's default mode (live, else off if disabled)."""
        if name == 'aggregator':
            return 'live'
        section = codec.engine(self.bot.settings, name)
        return 'live' if bool(section.get('enabled', False)) else 'off'
