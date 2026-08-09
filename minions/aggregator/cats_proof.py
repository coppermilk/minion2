"""Proof of work: the aggregator reacts to comments under its posts with cats.

Run it -- no network, no Telegram, no session -- and watch the REAL engine
(``cats.CatBrain``) and the REAL premium cat-emoji from
``aggregator_constants.json`` walk the whole path a comment takes:

    a post is watched  ->  a person comments under it  ->  the engine decides
    WHEN (human-timed) and WHICH cat  ->  a threaded reply carrying that
    premium cat-emoji is aimed at the comment, INSIDE the post's thread.

Nothing here mocks the decision code: ``schedule``, ``emit`` and ``is_comment``
are the same functions ``main.py`` calls in production. Only the Telethon send
is stood in for -- we print the exact payload (message text, the custom-emoji
entity, and the reply target) that ``main._send_cat`` would hand to Telethon.

    python -m minions.aggregator.cats_proof

Deterministic (seeded RNG + a fixed clock), so the output is reproducible and
the timing reads as a distracted human, not a scheduler. Source stays ASCII;
the cat glyphs are read from the JSON at runtime (BLUEPRINT 4).
"""

from __future__ import annotations

import json
import random
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from minions.aggregator import cats

# A deterministic seed and a fixed "now" so the proof reproduces byte for byte.
# Midday on a weekday, inside the active window, so cats are answered promptly.
_SEED = 0
_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC).timestamp()

# A stand-in channel discussion group and two of its posts (thread roots): the
# real target is a channel whose comments live in a linked discussion group
# (comments_in_discussion), so a comment is a reply whose thread root is the
# post. 1002 is the freshest post, 1001 the previous one.
_CHAT = -1002431466060
_POST_NEW = 1002
_POST_OLD = 1001


def _utf16_len(text: str) -> int:
    """UTF-16 code units -- Telegram's entity unit (see premium_emoji)."""
    return len(text.encode('utf-16-le')) // 2


def _local(ts: float, params: cats.CatParams) -> str:
    """A send time in the persona's timezone, to show it reads as human."""
    tz = timezone(timedelta(hours=params.tz_offset_hours))
    stamp = datetime.fromtimestamp(ts, tz=tz)
    return stamp.strftime('%Y-%m-%d %H:%M:%S (%a) %z')


def _load_params() -> cats.CatParams:
    """The REAL cat params + premium cat pool from the constants JSON."""
    path = Path(__file__).with_name('aggregator_constants.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    return cats.load_cat_params(data)


def _payload(spec: cats.CatEmoji, cat: cats.Cat) -> str:
    """The exact reply main._send_cat would send, rendered for the proof.

    A premium emoji is one visible glyph (the fallback) plus a custom-emoji
    entity pointing that span at the emoji's document id, and the reply is
    threaded: reply_to = the comment, top = the post's thread root -- so it
    lands INSIDE that comment's thread, not as a flat group message.
    """
    length = _utf16_len(spec.fallback)
    entity = (
        f'MessageEntityCustomEmoji(offset=0, length={length}, '
        f'document_id={spec.emoji_id})'
    )
    return (
        f'      text      : {spec.fallback!r}  (premium cat)\n'
        f'      entity    : {entity}\n'
        f'      reply_to  : {cat.reply_to}   (the comment)\n'
        f'      top_msg_id: {cat.root}   (the post thread -- keeps it in '
        f'the comments)\n'
        f'      peer      : {cat.chat}'
    )


def _comment(brain: cats.CatBrain, *, root: int, msg_id: int, person: str,  # noqa: PLR0913 -- a proof reads clearest with every field named at the call site
             text: str, engaged: bool) -> cats.Cat | None:
    """Run one comment through the real engine, exactly as main.py does.

    Mirrors main._maybe_cat/_schedule_comment: recognise the comment, key it
    once-per-(post, person), let the engine decide when, then (on a cat) emit
    which premium cat-emoji to send. Returns the scheduled Cat, or None.
    """
    if not brain.is_comment(_CHAT, root):
        print(f'  comment {msg_id} by {person}: NOT under a watched post '
              f'-> ignored')
        return None
    key = f'{_CHAT}:{root}:{person}'
    when = brain.schedule(key, engaged=engaged)
    if when is None:
        print(f'  comment {msg_id} by {person} under post {root}: '
              f'no cat (dedup / skip / silent day)')
        return None
    cat = cats.Cat(
        chat=_CHAT, reply_to=msg_id, root=root, when=when, text=text
    )
    brain.add_pending(cat)
    specs = brain.emit()
    params = brain.params
    print(f'  comment {msg_id} by {person} under post {root}: CAT scheduled')
    print(f'      when      : {_local(when, params)}   '
          f'(+{when - _NOW:.0f}s, jittered off :00)')
    for spec in specs:
        print(_payload(spec, cat))
    return cat


def main() -> None:
    """Drive the real engine end to end and print the proof."""
    params = _load_params()
    pool = ', '.join(f'{c.emoji_id}({c.fallback})' for c in params.pool)
    brain = cats.CatBrain(
        params,
        Path('/tmp/cats_proof_state.json'),  # noqa: S108 -- throwaway state
        random.Random(_SEED),  # noqa: S311 -- reproducible proof, not crypto
    )
    brain.clock = lambda: _NOW

    print('=' * 72)
    print('PROOF OF WORK -- cat reactions to comments under the last posts')
    print('=' * 72)
    print(f'engine enabled          : {params.enabled}')
    print(f'comments_in_discussion  : {params.comments_in_discussion} '
          f'(channel + linked discussion group)')
    print(f'watch_posts             : {params.watch_posts} '
          f'(only the last N posts are answered)')
    print(f'premium cat pool ({len(params.pool):>2})    : {pool}')
    print(f'now                     : {_local(_NOW, params)}')
    print()

    print('STEP 1  watch the last posts (main.backfill_cat_posts)')
    brain.note_post(_CHAT, _POST_OLD)
    brain.note_post(_CHAT, _POST_NEW)
    print(f'  watch-list: {brain.posts}')
    print(f'  is a reply to post {_POST_NEW} a comment? '
          f'{brain.is_comment(_CHAT, _POST_NEW)}')
    print(f'  is a reply to post 9999 a comment?      '
          f'{brain.is_comment(_CHAT, 9999)}')
    print()

    print('STEP 2  a person comments under the freshest post -> a cat')
    _comment(brain, root=_POST_NEW, msg_id=5001, person='alice', engaged=True,
             text='love this one!')
    print()

    print('STEP 3  once per (post, person): the same person, same post again')
    _comment(brain, root=_POST_NEW, msg_id=5002, person='alice', engaged=True,
             text='and again')
    print()

    print('STEP 4  a DIFFERENT person, same post -> eligible again')
    _comment(brain, root=_POST_NEW, msg_id=5003, person='bob', engaged=True,
             text='haha nice')
    print()

    print('STEP 5  the SAME person under a DIFFERENT post -> eligible again')
    _comment(brain, root=_POST_OLD, msg_id=5004, person='alice', engaged=False,
             text='saw this yesterday')
    print()

    print('STEP 6  a message that is not a comment on a watched post')
    _comment(brain, root=7777, msg_id=5005, person='carol', engaged=False,
             text='off-topic chatter')
    print()

    print('STEP 7  the queue that survives a restart (persisted pending cats)')
    for entry in brain.state.pending:
        print(f'  pending: reply_to={entry["reply_to"]} '
              f'root={entry["root"]} in {entry["chat"]}  "{entry["text"]}"')
    print()
    count = len(brain.state.pending)
    print(f'RESULT: {count} cat reply(ies) aimed at comments inside their '
          f'posts -- once per (post, person), human-timed.')
    print('=' * 72)


if __name__ == '__main__':
    main()
