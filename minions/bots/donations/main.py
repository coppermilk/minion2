"""donations bot: post each new donor alert, and serve the bed roster.

Platform-agnostic (BLUEPRINT 9 recipe): ``Feed``s supply the alerts,
this bot renders them in Russian -- who gave, how much, and their
question -- and posts them to one chat. Streamlabs and Revolut run
together (``DONATION_PLATFORM`` is a comma list). Tokenless or
unconfigured, the bot idles (REQ-DEG-001).

Alongside the alert dock runs a PUBLIC command dock: anyone may ask who
is "under the bed" (the donors of the last seven days) and the bot
renders the roster. The Russian templates live in ``messages.json``
(UTF-8 package data), so the repo-wide ASCII law holds for every ``.py``.
"""

from __future__ import annotations

import functools
import html
import json
import os
import time
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING
from typing import cast

from minion_core.adapters.admin import admin_config
from minion_core.adapters.donations import AlertSpec
from minion_core.adapters.donations import BedBroadcast
from minion_core.adapters.donations import BroadcastSpec
from minion_core.adapters.donations import DonationAlerts
from minion_core.adapters.donations import TgSender
from minion_core.adapters.donations import bed_roster
from minion_core.adapters.donations import feeds_for
from minion_core.adapters.files import free_quota
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgPublicCommands
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.adapters.donations import BedRoster
    from minion_core.adapters.donations import Donation
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'donations'

DEFAULT_PLATFORM = 'streamlabs'


def load_messages() -> dict[str, str]:
    """The Russian alert and bed templates, stored as UTF-8 package data."""
    pkg = resources.files(__package__)
    raw = (pkg / 'messages.json').read_text(encoding='utf-8')
    return cast('dict[str, str]', json.loads(raw))


def render(templates: Mapping[str, str], alert: Donation) -> str:
    """One alert as Russian HTML: who sent, how much, the question.

    The donor's name and message are HTML-escaped (they are untrusted
    input); the currency symbol and the "tip again" link are looked up by
    code and platform, so a Revolut gift links to Revolut, not Streamlabs.
    """
    name = html.escape(alert.name) or templates['anonymous']
    message = html.escape(alert.message) or templates['no_message']
    symbol = templates.get('cur_' + alert.currency, templates['cur_default'])
    link = templates.get('link_' + alert.platform.lower(), templates['link'])
    return templates['alert'].format(
        platform=html.escape(alert.platform),
        name=name,
        amount=html.escape(alert.amount),
        symbol=symbol,
        message=message,
        link=link,
    )


def render_bed(templates: Mapping[str, str], names: list[str]) -> str:
    """Who is under the bed right now, as a plain-text roster."""
    if not names:
        return templates['bed_empty']
    head = templates['bed_head'].format(count=len(names))
    lines = '\n'.join(
        templates['bed_line'].format(name=name or templates['anonymous'])
        for name in names
    )
    return head + '\n' + lines


@dataclass(frozen=True)
class _BedCommand:
    """The public command handler: answer the bed roster, else stay silent."""

    roster: BedRoster
    templates: Mapping[str, str]

    def __call__(self, text: str) -> str:
        """Render the roster when the text carries a bed trigger, else ''."""
        low = text.strip().lower()
        triggers = self.templates['bed_triggers'].split('|')
        if not any(word and word in low for word in triggers):
            return ''
        return render_bed(self.templates, self.roster.active(time.time()))


def _alerts(cfg: Settings, env: Mapping[str, str], api: TgApi) -> Stage:
    """The poll-and-post alert dock; chat and platforms are admin knobs."""
    templates = load_messages()
    admin = admin_config(cfg.state)
    platform = admin.effective(
        'donation_platform', env.get('DONATION_PLATFORM', DEFAULT_PLATFORM)
    )
    sender = TgSender(api, parse_mode='HTML', preview=False)
    spec = AlertSpec(
        chat=admin.effective('donation_chat', env.get('DONATION_CHAT', '')),
        state=cfg.state,
        render=functools.partial(render, templates),
        poll_sec=float(admin.get('donation_poll_sec')),
    )
    return DonationAlerts(feeds_for(platform, env), sender, spec)


def _commands(cfg: Settings, env: Mapping[str, str], api: TgApi) -> Stage:
    """The public "who is under the bed" command dock (any chat)."""
    handle = _BedCommand(bed_roster(cfg.state), load_messages())
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir(BOT), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.bot_dir(BOT),
        offset=cfg.state / f'{BOT}-cmd.offset',
        chats=chats_from(env),
    )
    return TgPublicCommands(api, spec, handle)


def _broadcast(cfg: Settings, env: Mapping[str, str], api: TgApi) -> Stage:
    """The timed bed-roster broadcast; interval and chat read live (admin)."""
    templates = load_messages()
    admin = admin_config(cfg.state)
    default_chat = admin.effective(
        'donation_chat', env.get('DONATION_CHAT', '')
    )
    spec = BroadcastSpec(
        chat=lambda: admin.get('bed_chat') or default_chat,
        cron=lambda: admin.get('bed_broadcast_cron'),
        render=functools.partial(render_bed, templates),
    )
    return BedBroadcast(bed_roster(cfg.state), TgSender(api), spec)


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Three docks on one belt: alerts, public commands, timed broadcast."""
    api = TgApi(env.get('TG_TOKEN', ''))
    return (
        _alerts(cfg, env, api)
        | _commands(cfg, env, api)
        | _broadcast(cfg, env, api)
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and service both docks forever (idle if unset)."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
