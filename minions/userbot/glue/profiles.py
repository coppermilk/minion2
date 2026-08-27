# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Per-service profile + live/test mode management, mixed into Userbot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from minions.userbot.core.base import UserbotProtocol
from minions.userbot.core.runtime import cancel
from minions.userbot.glue.commands import SERVICE_MODES
from minions.userbot.glue.commands import SERVICE_NAMES

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger('userbot')


class _ProfilesMixin(UserbotProtocol):
    """Per-service profile + live/test mode management, mixed into Userbot."""

    def _service_dir(self, name: str) -> Path:
        """Return (and create) the state dir for a service's OWN mode.

        Test state lives in ``base/test``, live (and off) in ``base``, so one
        service can sandbox its state while another stays live.
        """
        pdir = self._profile_dir(self._modes[name])
        pdir.mkdir(parents=True, exist_ok=True)
        return pdir

    def _profile_dir(self, mode: str) -> Path:
        """Return the state dir for MODE: base for live, base/test for test."""
        test = self._state_base / 'test'
        return test if mode == 'test' else self._state_base

    def _rescan_interval(self, mode: str) -> float:
        """Return the auto-rescan period for MODE: test is fast, live is slow.

        Test wants a tight loop while you iterate (default 5 min); live can be
        relaxed (default 1 hour). Both fall back to ``rescan_sec``.
        """
        cfg = self._raw.get('reactions')
        cfg = cfg if isinstance(cfg, dict) else {}
        default = float(cfg.get('rescan_sec', 300.0))
        key = 'rescan_sec_test' if mode == 'test' else 'rescan_sec_live'
        return float(cfg.get(key, default))

    def _profile_channel(self, mode: str) -> int:
        """Return the greeter's default channel for MODE (test = test chat)."""
        if mode == 'test':
            return self.config.test_target or self.config.source
        return self.config.targets[0] if self.config.targets else 0

    def _load_service_modes(self) -> dict[str, str]:
        """Load each service's mode from ``{"services": {name: mode}}``.

        A missing file, an unreadable one or a missing block (a fresh
        install) leaves every service on its own default -- see
        ``_default_mode``.
        """
        try:
            raw = json.loads(self._mode_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            raw = {}
        stored = raw.get('services')
        stored = stored if isinstance(stored, dict) else {}
        return {n: self._clean_mode(n, stored.get(n)) for n in SERVICE_NAMES}

    def _clean_mode(self, name: str, value: object) -> str:
        """Return a valid stored mode, else the service's default."""
        if value in SERVICE_MODES:
            return str(value)
        return self._default_mode(name)

    def _default_mode(self, name: str) -> str:
        """Return a service's default mode (live, else off if disabled)."""
        if name == 'aggregator':
            return 'live'
        return 'live' if self._feature_default(name) else 'off'

    def _save_service_modes(self) -> None:
        """Persist every service's mode so a restart resumes them."""
        self._mode_path.write_text(
            json.dumps({'services': self._modes}, indent=2), encoding='utf-8'
        )

    def _feature_default(self, name: str) -> bool:
        """Return a feature's JSON default ``enabled`` (its section flag)."""
        section = self._raw.get(name)
        section = section if isinstance(section, dict) else {}
        return bool(section.get('enabled', False))

    def _feature_enabled(self, name: str) -> bool:
        """Whether a service is active (its mode is not 'off')."""
        return self._modes.get(name, 'off') != 'off'

    async def start_profile(self, *, source_backfill: bool = True) -> None:
        """Hydrate the active profile and start its background loops.

        ``source_backfill`` re-scans the source for missed videos -- wanted at
        boot, but skipped on a live<->test switch so entering test never dumps
        a burst of recent videos into the test channel.
        """
        self.restore()
        try:
            self.rearm_reactions()
            await self.backfill_react_posts()
            await self.backfill_react_comments()
            if self.reactions.params.enabled:
                self.reactions.mark_alive(time.time())
        except Exception:
            log.exception('reactions: startup step failed; listening anyway')
        if source_backfill:
            await self.backfill()
        self._greeter_task = asyncio.create_task(self.greeter.loop())
        self._react_rescan_task = asyncio.create_task(self.react_rescan_loop())
        self._stories_task = asyncio.create_task(self.stories_loop())

    async def stop_profile(self) -> None:
        """Cancel the active profile's timers and loops (before a switch)."""
        self._cancel_react_tasks()
        self._cancel_story_tasks()
        self._cancel_enrich_tasks()
        for group in self.groups:
            cancel(getattr(group, 'task', None))
        cancel(self._greeter_task)
        cancel(self._react_rescan_task)
        cancel(self._stories_task)
        self._greeter_task = None
        self._react_rescan_task = None
        self._stories_task = None
        self.users.close()  # release the SQLite handle before a rebind

    def _cancel_enrich_tasks(self) -> None:
        """Cancel any in-flight identity-enrichment lookups."""
        for task in list(self._enrich_tasks):
            task.cancel()
        self._enrich_tasks.clear()

    def _cancel_story_tasks(self) -> None:
        """Cancel every in-flight story-view timer (before a mode switch)."""
        for task in list(self._story_tasks):
            task.cancel()
        self._story_tasks.clear()
        self._pending_views.clear()

    async def switch_mode(self, mode: str) -> None:
        """Switch every ACTIVE service to MODE (the /test and /live commands).

        A total sandbox, as before: the poster always follows, and any service
        currently on moves to MODE too; a service that is off stays off. Tear
        the profile down, rebind each service, hydrate. Persisted, so a restart
        comes up the same. Per-service overrides use ``set_service_mode``.
        """
        self._modes = {
            n: mode if (n == 'aggregator' or m != 'off') else 'off'
            for n, m in self._modes.items()
        }
        await self.stop_profile()
        self._build_profile()
        self._save_service_modes()
        await self.start_profile(source_backfill=False)
        labels = await self._chat_labels()
        dest = ', '.join(labels.get(t, str(t)) for t in self.live_targets())
        await self.client.send_message(
            self.config.source,
            f'Mode: {self.mode.upper()}. ALL posts now go to: {dest}',
        )
        log.info('mode -> %s, posting to %s', self.mode, self.live_targets())
