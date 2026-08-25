# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The chat command dispatcher, mixed into Aggregator.

Extracted from ``main``: the /command table and the feature on/off switches,
plus the small handlers that render straight back to the source chat
(/help, /emojis, /preview, /features, /test, /live, /greetnow). Handlers that
live on other mixins (cats, comod, users, stories) are dispatched through
``self``. ``_CommandsMixin`` inherits ``AggregatorProtocol`` (base.py) so the
type checker knows that shared surface.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from minions.aggregator.core.base import AggregatorProtocol
from minions.aggregator.core.render import _compose
from minions.aggregator.core.render import _render_constants
from minions.aggregator.core.render import _sample_groups

if TYPE_CHECKING:
    from telethon import events

log = logging.getLogger('aggregator')


# Chat commands (from ANY chat, ANYONE), always rendered into the source chat:
# /emojis previews the whole unified emoji array (all types) with ids; /preview
# renders sample posts (partial + full platform coverage) for QC; /status
# reports what is pending, what was posted/rejected, the last posts and the cat
# engine's live state; /requeue refreshes the pending-cat queue.
# /help (and its natural alias /start) print a plain-language command menu --
# the friendly front door for anyone who has never used the bot.
COMMAND_HELP = '/help'
COMMAND_START = '/start'
COMMAND_EMOJIS = '/emojis'
COMMAND_PREVIEW = '/preview'
COMMAND_STATUS = '/status'
# /requeue safely refreshes the pending-cat queue: cancel the in-flight timers
# and re-arm from the persisted queue (renewing any that are due).
COMMAND_REQUEUE = '/requeue'
# /catnow answers EVERY pending commenter immediately (bypass the human-like
# wait) -- an operator override for "reply to everyone now".
COMMAND_CATNOW = '/catnow'
# /greetnow forces the greeter to poll+process now (no waiting for poll_sec) --
# for testing welcome/farewell DMs.
COMMAND_GREETNOW = '/greetnow'
# /users prints the users-DB summary (audience totals, top commenters, recent
# join/leave) when the users database is enabled.
COMMAND_USERS = '/users'
# /stories prints the story-viewer log: how many stories were viewed and whose,
# most recent first (when the story viewer is enabled).
COMMAND_STORIES = '/stories'
# /comod manages the cabinet ("shkaf"): '/comod <nick> <amount>' moves a
# supporter onto a named shelf (a 30-day timer) and posts the rendered cabinet
# photo plus the move-in announcement; '/comod' alone re-posts the cabinet;
# '/comod kick <nick>' evicts by hand. Takes arguments, so it is matched by
# prefix, not by exact text like the others.
COMMAND_COMOD = '/comod'
# /propiska_shkaf_month prints the month's cabinet registry as text: one line
# per resident -- a random premium heart, the nick, and the move-in date.
COMMAND_PROPISKA = '/propiska_shkaf_month'
# /test and /live switch where posts go: /test routes ALL posts to TEST_CHAT_ID
# (a test channel), /live routes them back to the live targets. Persisted, so
# the mode survives a restart.
COMMAND_TEST = '/test'
COMMAND_LIVE = '/live'
# /features lists the toggleable features and whether each is on. Every feature
# also gets a runtime switch pair '/<name>_on' and '/<name>_off' (e.g.
# /stories_off) that flips its enabled flag, persists the choice (it survives a
# restart, overriding the JSON default), and restarts the profile's loops so
# the change takes effect at once. The togglable feature sections, by name:
COMMAND_FEATURES = '/features'
FEATURE_NAMES = ('cats', 'stories', 'users', 'greeter')
FEATURE_ON_SUFFIX = '_on'
FEATURE_OFF_SUFFIX = '_off'
# /services prints a table of every service and its mode, and each service
# takes '/<service> off|test|live' to sandbox it on its own (test = its own
# state, and for the poster/greeter its own destination) while others stay
# live. The poster ('aggregator') is a service too, so it finally has an off.
COMMAND_SERVICES = '/services'
SERVICE_NAMES = ('aggregator', *FEATURE_NAMES)
SERVICE_MODES = ('off', 'test', 'live')
_SERVICE_CMD_WORDS = 2  # '/<service> <mode>'


def _is_mode_arg(parts: list[str]) -> bool:
    """Whether ``parts`` is a '/<service> <mode>' with a valid mode word."""
    return len(parts) == _SERVICE_CMD_WORDS and parts[1] in SERVICE_MODES


class _CommandsMixin(AggregatorProtocol):
    """The /command dispatcher + feature switches, mixed into Aggregator."""

    async def _command(self, text: str) -> bool:
        """Run a matching /command, returning True if one handled the text."""
        # /comod carries arguments ('/comod <nick> <amount>'), so it is matched
        # by its leading word rather than by the exact-text table below.
        if text.split()[:1] == [COMMAND_COMOD]:
            await self.cabinet_command(text)
            return True
        # Feature switches: /features and every '/<name>_on|off'. Matched here,
        # before the exact table, because the name is dynamic (one pair per
        # feature) rather than a fixed command string.
        if await self._feature_command(text):
            return True
        # Service modes: /services and '/<service> off|test|live'. Matched here
        # (before the exact table) because '/<service> <mode>' carries an arg.
        if await self._service_command(text):
            return True
        handlers = {
            COMMAND_HELP: self.help_report,
            COMMAND_START: self.help_report,
            COMMAND_EMOJIS: self.show_constants,
            COMMAND_PREVIEW: self.preview_posts,
            COMMAND_STATUS: self.status_report,
            COMMAND_REQUEUE: self.requeue_cats,
            COMMAND_CATNOW: self.answer_all_now,
            COMMAND_GREETNOW: self.greet_now,
            COMMAND_USERS: self.users_report,
            COMMAND_STORIES: self.stories_report,
            COMMAND_PROPISKA: self.propiska_report,
            COMMAND_TEST: self.enter_test,
            COMMAND_LIVE: self.enter_live,
        }
        handler = handlers.get(text)
        if handler is None:
            return False
        await handler()
        return True

    async def _feature_command(self, text: str) -> bool:
        """Handle /features and any '/<name>_on|off', else return False.

        A toggle flips the feature's persisted override and restarts the
        profile so the change takes effect at once. An unknown feature name is
        left unhandled (so it falls through to the /help nudge).
        """
        parts = text.split(maxsplit=1)
        word = parts[0] if parts else ''
        if word == COMMAND_FEATURES:
            await self.features_report()
            return True
        for suffix, on in (
            (FEATURE_ON_SUFFIX, True),
            (FEATURE_OFF_SUFFIX, False),
        ):
            if word.endswith(suffix):
                name = word[1:-len(suffix)]  # strip '/' and the suffix
                if name in FEATURE_NAMES:
                    await self.switch_feature(name, on=on)
                    return True
        return False

    async def _service_command(self, text: str) -> bool:
        """Handle /services and '/<service> off|test|live', else False."""
        parts = text.split()
        word = parts[0] if parts else ''
        if word == COMMAND_SERVICES:
            await self.services_report()
            return True
        name = word[1:] if word.startswith('/') else ''
        if name in SERVICE_NAMES and _is_mode_arg(parts):
            await self.set_service_mode(name, parts[1])
            return True
        return False

    async def set_service_mode(self, name: str, mode: str) -> None:
        """Set one service's mode (off/test/live), persist, restart its loops.

        No-op-reports when already there; otherwise records the mode, tears the
        profile down and rebuilds it so this service's dir/enabled/destination
        take effect while the others keep their own modes.
        """
        if self._modes.get(name) == mode:
            await self.client.send_message(
                self.config.source, f'{name}: already {mode}'
            )
            return
        self._modes[name] = mode
        self._save_service_modes()
        await self.stop_profile()
        self._build_profile()
        await self.start_profile(source_backfill=False)
        log.info('service %s -> %s', name, mode)
        await self.client.send_message(self.config.source, f'{name}: {mode}')

    async def services_report(self) -> None:
        """Post the service table: each service's mode and switch command."""
        lines = ['Services']
        for name in SERVICE_NAMES:
            mode = self._modes.get(name, 'off')
            dot = self._dot(on=mode != 'off')
            lines.append(
                f'{self._bul()} {dot} {name}: {mode.upper()}'
                f'   /{name} off|test|live'
            )
        await self.client.send_message(self.config.source, '\n'.join(lines))
        log.info('sent services report to %s', self.config.source)

    async def _unknown_command(
        self, event: events.NewMessage.Event, text: str
    ) -> bool:
        """In the source chat, nudge a lone unknown /command toward /help."""
        if event.chat_id != self.config.source:
            return False
        if not text.startswith('/') or ' ' in text or not text[1:].isalpha():
            return False
        await self.client.send_message(
            self.config.source, self.consts.help_hint
        )
        return True

    async def switch_feature(self, name: str, *, on: bool) -> None:
        """Turn a feature on/off (a thin alias over ``set_service_mode``).

        ``_on`` brings it up in the aggregator's current mode, so toggling a
        feature on while the bot is testing joins the test sandbox; ``_off``
        sets it off. Granular per-service test/live uses ``/<service> <mode>``.
        """
        await self.set_service_mode(name, self.mode if on else 'off')

    async def features_report(self) -> None:
        """Post each feature's on/off state and its switch commands."""
        lines = ['Features']
        for name in FEATURE_NAMES:
            dot = self._dot(on=self._feature_enabled(name))
            cmds = f'/{name}_on  /{name}_off'
            lines.append(f'{self._bul()} {dot} {name}  {cmds}')
        await self.client.send_message(
            self.config.source, '\n'.join(lines)
        )
        log.info('sent features report to %s', self.config.source)

    async def help_report(self) -> None:
        """Send the plain-language command menu (/help and /start)."""
        await self.client.send_message(
            self.config.source, self.consts.help_text, link_preview=False
        )
        log.info('sent help menu to %s', self.config.source)

    async def show_constants(self) -> None:
        """Post a preview of the whole unified emoji array to the watcher."""
        message = _render_constants(self.consts)
        await self.client.send_message(
            self.config.source,
            message.text,
            formatting_entities=message.entities,
        )
        log.info('sent premium constants preview to %s', self.config.source)

    async def preview_posts(self) -> None:
        """Render QC sample posts (partial + full coverage) to the source."""
        groups = _sample_groups(self.consts)
        for group in groups:
            message = _compose(group, self.config.platforms, self.consts)
            await self.client.send_message(
                self.config.source,
                message.text,
                formatting_entities=message.entities,
                link_preview=False,
            )
        log.info(
            'sent %d QC preview posts to %s', len(groups), self.config.source
        )

    async def enter_test(self) -> None:
        """Switch the whole bot to the TEST profile (the /test command)."""
        await self.switch_mode('test')

    async def enter_live(self) -> None:
        """Switch the whole bot to the LIVE profile (the /live command)."""
        await self.switch_mode('live')

    async def greet_now(self) -> None:
        """Force the greeter to poll+process now (the /greetnow command)."""
        summary = await self.greeter.sync_now()
        await self.client.send_message(self.config.source, summary)
        log.info('greetnow: %s', summary)
