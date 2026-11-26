# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
r"""Dry-check a channel-edit package against a snapshot, without the NAS.

Run before the package's PR is merged (the merge is the approval)::

    python -m minions.userbot.dev.tgedit_check \\
        minions/userbot/tgedit/475-cleanup.json telegram.json

``telegram.json`` is the site MCP's ``telegram`` answer (``limit`` 1000):
the JSON-RPC envelope as curl saved it, or the tool's text. Every item is
dry-run by the engine's own planner (``core/tgedit``) and printed as
``post N . action -> matched / old not found / old more than once / no
post``, in the words of ``engines.tgedit.labels``.

The snapshot is the public web view: it has links but no entity offsets
and no channel description. Formatting cuts are therefore not seen here,
and ``desc`` reads ``unchecked`` -- the engine's own dry run in Telegram
remains the last word.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from minions.userbot.core import codec
from minions.userbot.core import config
from minions.userbot.core import relink
from minions.userbot.core import tgedit
from minions.userbot.glue.tgedit import RECENT
from minions.userbot.glue.tgedit import load_edit_params

UNCHECKED = 'unchecked'
"""Outcome of an item the snapshot cannot check (the description)."""
_UTF16 = 'utf-16-le'
_USAGE = 'usage: tgedit_check <package.json> <telegram snapshot>'


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
            outcomes.append(tgedit.Outcome(item, UNCHECKED))
        else:
            plan = plans.pop(item.post, None)
            outcomes += plan.outcomes if plan else ()
    return outcomes


def main(argv: list[str]) -> int:
    """Print the dry check; 1 when the package or snapshot is refused."""
    if len(argv) != 2:  # noqa: PLR2004 -- two paths
        sys.stderr.write(_USAGE + '\n')
        return 2
    package_path, snapshot_path = (Path(one) for one in argv)
    params = load_edit_params(config.read_json(config.CONSTANTS_PATH))
    try:
        package = tgedit.parse(
            package_path.read_text('utf-8'), package_path.stem
        )
        tgedit.check_limits(package.items, params.max_deletes)
        posts = read_snapshot(snapshot_path.read_text('utf-8'))
    except (tgedit.PackageError, SnapshotError) as exc:
        sys.stderr.write(f'refused: {exc}\n')
        return 1
    outcomes = check(package, posts, params.guard)
    labels = params.labels
    counts = tgedit.tally(outcomes)
    summary = ', '.join(
        f'{labels.get(k, k)} {v}' for k, v in sorted(counts.items())
    )
    lines = [f'{package.id}: {len(outcomes)} items, {summary}']
    lines += tgedit.report(outcomes, labels)
    sys.stdout.write('\n'.join(lines) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
