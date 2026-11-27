# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
r"""Dry-check the feed's packages against a snapshot, without the NAS.

Run before a package's PR is merged into the site (the merge is the
approval)::

    python -m minions.userbot.dev.tgedit_check \\
        https://daetotyoma.com/tgedit.json telegram.json

The feed is the site's ``/tgedit.json``: its address, or a file (the
site's ``dist/tgedit.json`` before the merge). Every package in it is
checked, one block each.

``telegram.json`` is the site MCP's ``telegram`` answer (``limit`` 1000):
the JSON-RPC envelope as curl saved it, or the tool's text. Every item is
dry-run by the engine's own planner (``core/tgedit``) and printed as
``post N . action -> matched / old not found / old more than once / no
post``, in the words of ``engines.tgedit.labels``.

The snapshot is the public web view: it has links but no entity offsets,
no premium emoji and no channel description. Formatting cuts are
therefore not seen here, and an edit the premium-emoji rule would stop on
a bare post reads ``unchecked`` (the post may carry emoji the view does
not show); a ``desc`` is refused by the rule whatever the description.
The engine's own dry run in Telegram remains the last word.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from minion_core.adapters import feed as feeds
from minions.userbot.core import codec
from minions.userbot.core import config
from minions.userbot.core import relink
from minions.userbot.core import tgedit
from minions.userbot.glue.tgedit import RECENT
from minions.userbot.glue.tgedit import load_edit_params

UNCHECKED = 'unchecked'
"""Outcome of an item the snapshot cannot check (premium emoji unseen)."""
_UTF16 = 'utf-16-le'
_USAGE = 'usage: tgedit_check <feed: file or URL> <telegram snapshot>'


class SnapshotError(ValueError):
    """The snapshot is not a ``telegram`` answer; the message says why."""


def read_snapshot(text: str) -> list[dict[str, object]]:
    """Return the snapshot's posts, newest first.

    Accepts the JSON-RPC envelope or the tool's text (a summary line, then
    the JSON object).
    """
    raw = _unwrap(_json(text))
    if raw is None:
        raw = _json(text[text.find('{') :]) if '{' in text else None
    posts = raw.get('posts') if isinstance(raw, dict) else None
    if not isinstance(posts, list):
        msg = 'the snapshot has no posts list (is it a telegram answer?)'
        raise SnapshotError(msg)
    return [one for one in posts if isinstance(one, dict)]


def _json(text: str) -> object:
    """Parse ``text``; None when it is not JSON."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _unwrap(raw: object) -> object:
    """Open a JSON-RPC envelope down to the tool's object (None if broken)."""
    if not isinstance(raw, dict) or not isinstance(raw.get('result'), dict):
        return raw
    content = codec.rows(raw['result'].get('content'))
    first = content[0] if content and isinstance(content[0], dict) else {}
    text = str(first.get('text', ''))
    return _json(text[text.find('{') :]) if '{' in text else None


def _units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units."""
    return len(text.encode(_UTF16)) // 2


def to_post(raw: dict[str, object]) -> tgedit.Post:
    """Convert one snapshot post into the planner's post.

    A text link is placed where its words next occur in the text, so the
    "watch" rows and the chat stay protected here as in the engine; a link
    whose words are not in the text (a preview card, a button) is left out
    -- the engine cannot edit those either.
    """
    text = str(raw.get('text') or '')
    spans: list[tuple[int, int]] = []
    links: list[tuple[int, str]] = []
    cursor = 0
    for link in codec.rows(raw.get('links')):
        if not isinstance(link, dict) or link.get('kind') != 'text':
            continue
        words = str(link.get('text') or '')
        at = text.find(words, cursor) if words else -1
        if at < 0:
            continue
        links.append((len(spans), str(link.get('href') or '')))
        spans.append((_units(text[:at]), _units(words)))
        cursor = at + len(words)
    photos = raw.get('photos')
    return tgedit.Post(
        id=int(str(raw.get('id'))),
        text=text,
        spans=tuple(spans),
        kinds=(relink.HIDDEN,) * len(spans),
        links=tuple(links),
        media=bool(raw.get('video')) or bool(photos),
    )


def check(
    package: tgedit.Package,
    posts: list[dict[str, object]],
    guard: tgedit.Guard,
) -> list[tgedit.Outcome]:
    """Dry-run every item of ``package`` against the snapshot, in order."""
    by_id = {int(str(one.get('id'))): one for one in posts}
    plans = {
        post_id: tgedit.plan_post(
            to_post(by_id[post_id]) if post_id in by_id else None,
            group,
            guard,
        )
        for post_id, group in tgedit.by_post(package.items).items()
    }
    recent = [str(one.get('text') or '') for one in posts[:RECENT]]
    outcomes: list[tgedit.Outcome] = []
    for item in package.items:
        if item.action == tgedit.PIN_NEW:
            why = tgedit.plan_pin(recent, item, guard)
            outcomes.append(
                tgedit.Outcome(item, tgedit.SKIPPED, why)
                if why
                else tgedit.Outcome(item, tgedit.DRY_OK)
            )
        elif item.action == tgedit.DESC:
            # Every reason but ALREADY holds whatever the about is now.
            why = tgedit.plan_about('', item, guard)[1]
            outcomes.append(tgedit.Outcome(item, tgedit.SKIPPED, why))
        else:
            plan = plans.pop(item.post, None)
            outcomes += [_unseen(one) for one in plan.outcomes] if plan else []
    return outcomes


def _unseen(outcome: tgedit.Outcome) -> tgedit.Outcome:
    """Turn a premium-emoji skip of an edit into ``unchecked``.

    The snapshot shows no premium emoji, so every post reads bare here.
    """
    if outcome.reason != tgedit.NO_PREMIUM_EMOJI:
        return outcome
    return tgedit.Outcome(outcome.item, UNCHECKED)


def read_feed(where: str) -> str:
    """Return the feed's text from an ``http(s)`` address or a file."""
    if where.startswith(('https://', 'http://')):
        return feeds.read(where)
    return Path(where).read_text('utf-8')


def lines(
    package: tgedit.Package,
    outcomes: list[tgedit.Outcome],
    labels: dict[str, str],
) -> list[str]:
    """Return one package's block: the tally, then one line per item."""
    counts = tgedit.tally(outcomes)
    summary = ', '.join(
        f'{labels.get(k, k)} {v}' for k, v in sorted(counts.items())
    )
    head = f'{package.id}: {len(outcomes)} items, {summary}'
    return [head, *tgedit.report(outcomes, labels)]


def main(argv: list[str]) -> int:
    """Print the dry check; 1 when the feed or the snapshot is refused."""
    if len(argv) != 2:  # noqa: PLR2004 -- the feed and the snapshot
        sys.stderr.write(_USAGE + '\n')
        return 2
    where, snapshot_path = argv
    params = load_edit_params(config.read_json(config.CONSTANTS_PATH))
    try:
        feed = tgedit.parse_feed(read_feed(where))
        rules = feed.rules.tighten(params.max_deletes, params.pause_sec)
        for package in feed.packages:
            tgedit.check_limits(package.items, rules.max_deletes)
        posts = read_snapshot(Path(snapshot_path).read_text('utf-8'))
    except (
        feeds.FeedUnreachableError,
        tgedit.FeedError,
        tgedit.PackageError,
        SnapshotError,
        OSError,
    ) as exc:
        sys.stderr.write(f'refused: {exc}\n')
        return 1
    out: list[str] = []
    for package in feed.packages:
        outcomes = check(package, posts, params.guard)
        out += lines(package, outcomes, params.labels)
    sys.stdout.write('\n'.join(out) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
