# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The chat command dispatcher: text in, the service that answers it out.

Holds the /command table, the per-service mode switches, and the few small
handlers that render straight back to the source chat (/help, /emojis,
/preview, /test, /live, /greetnow). Everything else is a one-line hand-off,
so the table doubles as a map of which service owns which command.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from minions.userbot.core.render import compose
from minions.userbot.core.render import render_constants
from minions.userbot.core.render import sample_groups

if TYPE_CHECKING:
    from minion_core.adapters import userchat
    from minions.userbot.main import Userbot

log = logging.getLogger('userbot')


# Chat commands (from ANY chat, ANYONE), always rendered into the source chat:
# /emojis previews the whole unified emoji array (all types) with ids; /preview
# renders sample posts (partial + full platform coverage) for QC; /status
# reports what is pending, what was posted/rejected, the last posts and the
# reaction
# engine's live state; /requeue refreshes the pending-reaction queue.
# /help (and its natural alias /start) print a plain-language command menu --
# the friendly front door for anyone who has never used the bot.
COMMAND_HELP = '/help'
COMMAND_START = '/start'
COMMAND_EMOJIS = '/emojis'
COMMAND_PREVIEW = '/preview'
COMMAND_STATUS = '/status'
# /requeue safely refreshes the pending-reaction queue: cancel the in-flight
# timers
# and re-arm from the persisted queue (renewing any that are due).
COMMAND_REQUEUE = '/requeue'
# /reactnow answers EVERY pending commenter immediately (bypass the human-like
# wait) -- an operator override for "reply to everyone now".
COMMAND_REACTNOW = '/reactnow'
# /greetnow forces the greeter to poll+process now (no waiting for poll_sec) --
# for testing welcome/farewell DMs.
COMMAND_GREETNOW = '/greetnow'
# /users prints the users-DB summary (audience totals, top commenters, recent
# join/leave) when the users database is enabled.
COMMAND_USERS = '/users'
# /stories prints the story-viewer log: how many stories were viewed and whose,
# most recent first (when the story viewer is enabled).
COMMAND_STORIES = '/stories'
# /who prints one person's relationship history: '/who @name' or '/who <id>'.
# Every act with them, newest first, from the contact log -- the counters in
# /status are running totals OF this, so it is where a surprising percentage
# is checked against what actually happened. Takes an argument, so it is
# matched by prefix, not by exact text.
COMMAND_WHO = '/who'
# /people prints the roster: everyone we have a relationship with, most
# recently touched first, each with the ONE WORD for what we are doing with
# them now and where they are on their own curve. It is the middle view
# nothing answered -- /status shows the few with stories up this minute,
# /who shows one person's every act, and this is the list.
COMMAND_PEOPLE = '/people'
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
# /services and its older name /features both render the WHOLE /status, which
# is where the service table lives; neither prints a table of its own any more.
COMMAND_FEATURES = '/features'
COMMAND_SERVICES = '/services'
# Every service takes '/<service>_<action>' where action is on|off|test|live
# (on aliases live) -- so one service can be sandboxed on its own (test = its
# own state, and for the poster/greeter its own destination) while the others
# stay live. A toggle persists, overriding the JSON default, and restarts the
# profile's loops so the change takes effect at once. The poster ('aggregator')
# is a service too, so it finally has an off. Underscore form (not
# '/<service> <mode>') so Telegram renders each as a single tappable command.
SERVICE_NAMES = ('aggregator', 'reactions', 'stories', 'users', 'greeter')
SERVICE_MODES = ('off', 'test', 'live')
# Tap-command action word -> the mode it sets (rendered in this order).
SERVICE_ACTIONS = {'on': 'live', 'off': 'off', 'test': 'test', 'live': 'live'}


def _service_action(word: str) -> tuple[str, str] | None:
    """Parse a '/<service>_<action>' command word into (service, mode)."""
    for action, mode in SERVICE_ACTIONS.items():
        suffix = '_' + action
        if word.endswith(suffix):
            name = word[1 : -len(suffix)]  # strip '/' and '_<action>'
            if name in SERVICE_NAMES:
                return name, mode
    return None


class CommandRouter:
    """Route a /command to whichever service answers it.

    Holds the bot rather than sharing its ``self``: every handler below
    names the service it reaches, so the command table doubles as a map of
    which service owns which command.
    """

    def __init__(self, bot: Userbot) -> None:
        """Bind the bot whose services this router dispatches to."""
        self.bot = bot

    def _reaction_alias(self, text: str) -> str:
        """Map a persona label's friendly reaction commands to the canonical.

        The reaction commands are neutral (/reactnow and /reactions_on|off|
        test|live). A persona MAY set ``reactions.label`` (e.g. "cat") to also
        answer under a friendly name -- /catnow, /cat_on|off|test|live -- so it
        speaks its own vocabulary without hard-coding it. No label = neutral
        commands only.
        """
        label = self.bot.reactions.params.label
        if not label:
            return text
        parts = text.split(maxsplit=1)
        word = parts[0] if parts else text
        if word == f'/{label}now':
            return COMMAND_REACTNOW
        for action in ('on', 'off', 'test', 'live'):
            if word == f'/{label}_{action}':
                return f'/reactions_{action}'
        return text

    async def handle(self, text: str) -> bool:
        """Run a matching /command, returning True if one handled the text."""
        text = self._reaction_alias(text)  # persona label -> canonical, if set
        # /comod carries arguments ('/comod <nick> <amount>'), so it is matched
        # by its leading word rather than by the exact-text table below.
        if text.split()[:1] == [COMMAND_COMOD]:
            await self.bot.cabinet.command(text)
            return True
        if text.split()[:1] == [COMMAND_WHO]:
            await self.who_report(text)
            return True
        # /services, /features and every '/<service>_<action>' tap command.
        # Matched here (before the exact table) because the service name is
        # dynamic rather than a fixed command string.
        if await self._service_command(text):
            return True
        handlers = {
            COMMAND_HELP: self.help_report,
            COMMAND_START: self.help_report,
            COMMAND_EMOJIS: self.show_constants,
            COMMAND_PREVIEW: self.preview_posts,
            COMMAND_STATUS: self.bot.status_report,
            COMMAND_REQUEUE: self.bot.comment_watch.requeue,
            COMMAND_REACTNOW: self.bot.comment_watch.answer_now,
            COMMAND_GREETNOW: self.greet_now,
            COMMAND_USERS: self.bot.audience.report,
            COMMAND_STORIES: self.bot.story_watch.report,
            COMMAND_PEOPLE: self.people_report,
            COMMAND_PROPISKA: self.bot.cabinet.propiska,
            COMMAND_TEST: self.enter_test,
            COMMAND_LIVE: self.enter_live,
        }
        handler = handlers.get(text)
        if handler is None:
            return False
        await handler()
        return True

    async def _service_command(self, text: str) -> bool:
        """Handle /services, /features and '/<service>_<action>', else False.

        Actions are on|off|test|live (on aliases live). One toggle rebuilds
        only what changed; an unknown name falls through to the /help nudge.
        """
        words = text.split()
        word = words[0] if words else ''
        if word in (COMMAND_SERVICES, COMMAND_FEATURES):
            # The service table lives inside /status now, so both shortcuts
            # render the full status (which carries the tap-command table).
            await self.bot.status_report()
            return True
        parsed = _service_action(word)
        if parsed is None:
            return False
        await self.bot.modes.set(*parsed)
        return True

    async def nudge_unknown(self, msg: userchat.Msg, text: str) -> bool:
        """In the source chat, nudge a lone unknown /command toward /help."""
        if msg.chat_id != self.bot.config.source:
            return False
        if not text.startswith('/') or ' ' in text or not text[1:].isalpha():
            return False
        await self.bot.say(self.bot.consts.help_hint)
        return True

    async def help_report(self) -> None:
        """Send the plain-language command menu (/help and /start)."""
        await self.bot.say(self.bot.consts.help_text)
        log.info('sent help menu to %s', self.bot.config.source)

    async def show_constants(self) -> None:
        """Post a preview of the whole unified emoji array to the watcher."""
        message = render_constants(self.bot.consts)
        await self.bot.show(message)
        log.info(
            'sent premium constants preview to %s', self.bot.config.source
        )

    async def preview_posts(self) -> None:
        """Render QC sample posts (partial + full coverage) to the source."""
        groups = sample_groups(self.bot.consts)
        for group in groups:
            message = compose(
                group, self.bot.config.platforms, self.bot.consts
            )
            await self.bot.show(message)
        log.info(
            'sent %d QC preview posts to %s',
            len(groups),
            self.bot.config.source,
        )

    async def enter_test(self) -> None:
        """Switch the whole bot to the TEST profile (the /test command)."""
        await self.bot.modes.switch_all('test')

    async def enter_live(self) -> None:
        """Switch the whole bot to the LIVE profile (the /live command)."""
        await self.bot.modes.switch_all('live')

    async def greet_now(self) -> None:
        """Force the greeter to poll+process now (the /greetnow command)."""
        summary = await self.bot.greeter.sync_now()
        await self.bot.say(summary)
        log.info('greetnow: %s', summary)

    async def people_report(self) -> None:
        """Print the roster and what we do with each of them (/people)."""
        await self.bot.say(self.bot.report.people())

    async def who_report(self, text: str) -> None:
        """Print one person's whole relationship history (the /who command).

        Reads the contact log rather than the counters: /status shows the
        running totals, and this is what they are totals OF. A percentage
        that looks wrong is settled here, by looking at what happened.
        """
        await self.bot.say(self.bot.report.who(text.split(maxsplit=1)))
