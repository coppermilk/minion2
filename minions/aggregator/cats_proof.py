"""Proof of work: the aggregator reacts to comments under its posts with cats.

Run it -- no network, no Telegram, no session -- and watch the REAL engine
(``cats.CatBrain``) and the REAL premium cat-emoji from
``aggregator_constants.json`` walk the whole path a comment takes:

    a post goes out  ->  a cat REACTION lands on the post right away  ->  a
    person comments under it  ->  the engine decides WHEN (human-timed) and
    WHICH cat  ->  a custom-emoji cat REACTION is placed ON the comment message
    (a reaction pill under it), NOT a reply in the thread.

New posts are reacted to IMMEDIATELY (no human-like wait) -- they are ours, so
the cat goes on straight away; comments keep the distracted-human timing.

Nothing here mocks the decision code: ``schedule``, ``emit`` and ``is_comment``
are the same functions ``main.py`` calls in production. Only the Telethon send
is stood in for -- we print the exact payload (the ``SendReaction`` request
with its ``ReactionCustomEmoji`` and the target comment id) that
``main._send_cats`` would hand to Telethon.

    python -m minions.aggregator.cats_proof

Deterministic (seeded RNG + a fixed clock), so the output is reproducible and
the timing reads as a distracted human, not a scheduler. Source stays ASCII;
the cat glyphs are read from the JSON at runtime (BLUEPRINT 4).
"""

from __future__ import annotations

import json
import random
import tempfile
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from minions.aggregator import cats

# A deterministic seed and a fixed "now" so the proof reproduces byte for byte.
# Midday on a weekday, inside the active window, so cats are answered promptly.
_SEED = 1
_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC).timestamp()

# The target CHANNEL (posts live here; a post reaction goes on the channel
# message) and its linked DISCUSSION group (comments live here; a comment
# reaction goes on the comment message). comments_in_discussion is True, so a
# comment is a reply whose thread root is the post. 1002 is the freshest post's
# thread, 1001 the previous one.
_CHANNEL = -1002431466060
_CHAT = -1004402620527
_POST_NEW = 1002
_POST_OLD = 1001

# Throwaway state for the run, in the OS temp dir (works on Windows and
# POSIX alike -- a hardcoded /tmp resolves to \tmp on Windows and crashes
# the atomic save). Cleared at the start of main() so every run is fresh
# and reproducible, not replaying a previous run's dedup state.
_STATE = Path(tempfile.gettempdir()) / 'cats_proof_state.json'


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


def _payload(specs: list[cats.CatEmoji], cat: cats.Cat) -> str:
    """The exact reaction main._send_cats would send, rendered for the proof.

    A cat reaction is a ``ReactionCustomEmoji`` pointing at the emoji's
    document id; the whole set is placed ON the comment message (msg_id =
    the comment) in ONE ``SendReaction`` call -- a reaction pill under the
    comment, not a reply in the thread. A Premium account may hold more than
    one, so the rare second cat rides the same request.
    """
    glyphs = ' '.join(s.fallback for s in specs)
    reactions = ', '.join(
        f'ReactionCustomEmoji(document_id={s.emoji_id})' for s in specs
    )
    return (
        f'      request  : SendReaction(peer={cat.chat}, '
        f'msg_id={cat.reply_to}, add_to_recent=True)\n'
        f'      reaction : [{reactions}]\n'
        f'      shows as : {glyphs}   (a reaction pill ON comment '
        f'{cat.reply_to}, under post {cat.root})'
    )


def _post_reaction(brain: cats.CatBrain, channel: int, post_id: int) -> None:
    """Immediately react to a freshly-created post (main._react_to_post).

    No schedule, no wait -- the post is ours, so ``_react`` emits and the cat
    goes straight onto the channel message.
    """
    specs = brain.emit()
    reactions = ', '.join(
        f'ReactionCustomEmoji(document_id={s.emoji_id})' for s in specs
    )
    glyphs = ' '.join(s.fallback for s in specs)
    print(f'  new post {post_id} in channel {channel}: CAT REACTION now '
          f'(immediate, no wait)')
    print(f'      request  : SendReaction(peer={channel}, msg_id={post_id}, '
          f'add_to_recent=True)')
    print(f'      reaction : [{reactions}]')
    print(f'      shows as : {glyphs}   (a reaction pill ON the post itself)')


def _comment(brain: cats.CatBrain, *, root: int, msg_id: int, person: str,  # noqa: PLR0913 -- a proof reads clearest with every field named at the call site
             text: str, engaged: bool) -> cats.Cat | None:
    """Run one comment through the real engine, exactly as main.py does.

    Mirrors main._maybe_cat/_schedule_comment: recognise the comment, key it
    once-per-(post, person), let the engine decide when, then (on a cat) emit
    which premium cat-emoji to react with. Returns the scheduled Cat, or None.
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
    print(f'  comment {msg_id} by {person} under post {root}: CAT REACTION '
          f'scheduled')
    print(f'      when     : {_local(when, brain.params)}   '
          f'(+{when - _NOW:.0f}s, jittered off :00)')
    print(_payload(specs, cat))
    return cat


def main() -> None:
    """Drive the real engine end to end and print the proof."""
    params = _load_params()
    pool = ', '.join(f'{c.emoji_id}({c.fallback})' for c in params.pool)
    _STATE.unlink(missing_ok=True)  # start fresh: don't replay old dedup state
    brain = cats.CatBrain(
        params,
        _STATE,
        random.Random(_SEED),  # noqa: S311 -- reproducible proof, not crypto
    )
    brain.clock = lambda: _NOW

    print('=' * 72)
    print('PROOF OF WORK -- cat REACTIONS on new posts and on their comments')
    print('=' * 72)
    print(f'engine enabled          : {params.enabled}')
    print(f'comments_in_discussion  : {params.comments_in_discussion} '
          f'(channel + linked discussion group)')
    print(f'watch_posts             : {params.watch_posts} '
          f'(only the last N posts are answered)')
    print(f'premium cat pool ({len(params.pool):>2})    : {pool}')
    print(f'now                     : {_local(_NOW, params)}')
    print()

    print('STEP 1  a new post goes out -> react ON the post immediately')
    _post_reaction(brain, _CHANNEL, 500)
    print()

    print('STEP 2  watch the last posts (main.backfill_cat_posts)')
    brain.note_post(_CHAT, _POST_OLD)
    brain.note_post(_CHAT, _POST_NEW)
    print(f'  watch-list: {brain.posts}')
    print(f'  is a reply to post {_POST_NEW} a comment? '
          f'{brain.is_comment(_CHAT, _POST_NEW)}')
    print(f'  is a reply to post 9999 a comment?      '
          f'{brain.is_comment(_CHAT, 9999)}')
    print()

    print('STEP 3  a person comments under the freshest post -> react on it')
    _comment(brain, root=_POST_NEW, msg_id=5001, person='alice', engaged=True,
             text='love this one!')
    print()

    print('STEP 4  once per (post, person): the same person, same post again')
    _comment(brain, root=_POST_NEW, msg_id=5002, person='alice', engaged=True,
             text='and again')
    print()

    print('STEP 5  a DIFFERENT person, same post -> eligible again')
    _comment(brain, root=_POST_NEW, msg_id=5003, person='bob', engaged=True,
             text='haha nice')
    print()

    print('STEP 6  the SAME person under a DIFFERENT post -> eligible again')
    _comment(brain, root=_POST_OLD, msg_id=5004, person='alice', engaged=False,
             text='saw this yesterday')
    print()

    print('STEP 7  a message that is not a comment on a watched post')
    _comment(brain, root=7777, msg_id=5005, person='carol', engaged=False,
             text='off-topic chatter')
    print()

    print('STEP 8  the queue that survives a restart (persisted pending cats)')
    for entry in brain.state.pending:
        print(f'  pending: react on comment {entry["reply_to"]} '
              f'(post {entry["root"]}) in {entry["chat"]}  "{entry["text"]}"')
    print()
    count = len(brain.state.pending)
    print(f'RESULT: the new post got a cat reaction immediately, and {count} '
          f'cat reaction(s) are queued ON comments -- once per (post, '
          f'person), human-timed.')
    print('=' * 72)


if __name__ == '__main__':
    main()
