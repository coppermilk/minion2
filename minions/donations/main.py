"""donations bot: post each new donor alert to a Telegram chat.

Platform-agnostic (BLUEPRINT 9 recipe): a ``Feed`` supplies the alerts,
this bot renders them in Russian -- who gave, how much, and their
question -- and posts them to one chat. Streamlabs is the first feed;
other platforms slot in behind ``feed_for`` with no change here.
Tokenless or unconfigured, the bot idles (REQ-DEG-001).

The Russian templates live in ``messages.json`` (UTF-8 package data),
kept out of the ASCII-checked sources on purpose (BLUEPRINT 4): the
repo-wide ASCII law holds for every ``.py`` while the alerts still read
in Russian.
"""

from __future__ import annotations

import functools
import html
import json
import os
from importlib import resources
from typing import TYPE_CHECKING
from typing import cast

from minion_core.adapters.donations import AlertSpec
from minion_core.adapters.donations import DonationAlerts
from minion_core.adapters.donations import TgSender
from minion_core.adapters.donations import feed_for
from minion_core.adapters.tg import TgApi
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.adapters.donations import Donation
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'donations'

DEFAULT_PLATFORM = 'streamlabs'
DEFAULT_POLL_SEC = '10'


def load_messages() -> dict[str, str]:
    """The Russian alert templates, stored as UTF-8 package data."""
    pkg = resources.files(__package__)
    raw = (pkg / 'messages.json').read_text(encoding='utf-8')
    return cast('dict[str, str]', json.loads(raw))


def render(templates: Mapping[str, str], alert: Donation) -> str:
    """One alert as Russian HTML: who sent, how much, the question.

    The donor's name and message are HTML-escaped (they are untrusted
    input) before landing in the italic line and the bed; the currency
    symbol is looked up per code, so an amount reads ``500RUB`` -> the
    ruble sign, ``5USD`` -> a dollar sign, and so on.
    """
    name = html.escape(alert.name) or templates['anonymous']
    message = html.escape(alert.message) or templates['no_message']
    symbol = templates.get('cur_' + alert.currency, templates['cur_default'])
    return templates['alert'].format(
        name=name,
        amount=html.escape(alert.amount),
        symbol=symbol,
        message=message,
    )


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the poll-and-post dock; secrets come from ``env``."""
    templates = load_messages()
    feed = feed_for(env.get('DONATION_PLATFORM', DEFAULT_PLATFORM), env)
    sender = TgSender(
        TgApi(env.get('TG_TOKEN', '')), parse_mode='HTML', preview=False
    )
    spec = AlertSpec(
        chat=env.get('DONATION_CHAT', ''),
        offset=cfg.state / f'{BOT}.offset',
        render=functools.partial(render, templates),
        poll_sec=float(env.get('DONATION_POLL_SEC', DEFAULT_POLL_SEC)),
    )
    return DonationAlerts(feed, sender, spec)


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once and service the feed forever (idle if unset)."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
