"""props bot: read a scenario, recommend props, check what you have.

A query bot (replies in chat, no belt jobs). It answers a Telegram
message with the props the scenario calls for, split into what the
library already holds and what still needs preparing:

1. the model (local Qwen or Gemini, per the switch) lists required
   props from the scenario (``llm.list_props``);
2. each is matched against the ``Pr*`` prims already in ``pictures/``
   -- by name, then by CLIP text->image similarity for near-synonyms.

Input is BOTH: a long message is taken as the scenario; a short one
(or none) falls back to this week's script (``scripts.script_hint``).
Lighting/lamps and other categories can be added later by reusing the
same list-then-match shape.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

from minion_core.adapters.backend import select_backend
from minion_core.adapters.files import free_quota
from minion_core.adapters.files import usd_prim
from minion_core.adapters.llm import LlmError
from minion_core.adapters.llm import list_props
from minion_core.adapters.scripts import script_hint
from minion_core.adapters.tg import SpoolSpec
from minion_core.adapters.tg import TgApi
from minion_core.adapters.tg import TgCommands
from minion_core.adapters.tg import TgSpec
from minion_core.adapters.tg import chats_from
from minion_core.adapters.vision import EmbeddingCache
from minion_core.adapters.vision import embed_image
from minion_core.adapters.vision import embed_text
from minion_core.adapters.vision import nearest_named
from minion_core.adapters.vision import warm_embedder
from minion_core.kernel import run
from minion_core.settings import load

if TYPE_CHECKING:
    from collections.abc import Mapping

    from minion_core.adapters.vision import Vector
    from minion_core.kernel import Stage
    from minion_core.settings import Settings

BOT = 'props'

MIN_SCRIPT_CHARS = 40
"""A message this long is the scenario itself; shorter -> weekly script."""

HAVE_TAU = 0.24
"""Cosine floor for a text->image prop match (approximate; tunable)."""


def _owned_props(cfg: Settings) -> dict[str, Vector]:
    """Every ``Pr``-prefixed prim in the library, name -> vector."""
    library = EmbeddingCache(cfg).refresh(cfg.pictures, embed_image)
    owned: dict[str, Vector] = {}
    for key, vec in library.items():
        name = key.split('|', 1)[-1]
        if name.startswith('Pr'):
            owned[name] = vec
    return owned


def _semantic(req: str, owned: dict[str, Vector]) -> str:
    """The nearest owned prop above the threshold, or '' (need it)."""
    if not owned:
        return ''
    name, sim = nearest_named(embed_text(req), owned)
    return name if sim >= HAVE_TAU else ''


def _split(
    required: list[str], owned: dict[str, Vector]
) -> tuple[list[str], list[str]]:
    """Partition required props into have (matched name) and need."""
    have: list[str] = []
    need: list[str] = []
    for req in required:
        want = 'Pr' + usd_prim(req)
        if want in owned:
            have.append(want)
            continue
        matched = _semantic(req, owned)
        if matched:
            have.append(matched)
        else:
            need.append(req)
    return have, need


def _report(have: list[str], need: list[str]) -> str:
    """One reply: what the library has vs. what to prepare."""
    lines: list[str] = []
    if have:
        lines.append('Have: ' + ', '.join(dict.fromkeys(have)))
    if need:
        lines.append('Need: ' + ', '.join(dict.fromkeys(need)))
    return '\n'.join(lines) if lines else 'no props found in the scenario'


def respond(cfg: Settings, env: Mapping[str, str], text: str) -> str:
    """Map a message to a props report (pasted scenario or weekly)."""
    typed = text.strip()
    script = typed if len(typed) >= MIN_SCRIPT_CHARS else script_hint(cfg)
    if not script:
        return (
            'no scenario: paste the script, or drop the week .gdoc in _inbox'
        )
    try:
        required = list_props(script, select_backend(cfg, env))
    except LlmError as exc:
        return f'model error: {exc}'
    if not required:
        return 'no props found in the scenario'
    return _report(*_split(required, _owned_props(cfg)))


def build(cfg: Settings, env: Mapping[str, str]) -> Stage:
    """Assemble the command dock; cfg + env are bound into the handler."""
    api = TgApi(env.get('TG_TOKEN', ''))
    spec = TgSpec(
        spool=SpoolSpec(
            into=cfg.bot_dir(BOT), budget=functools.partial(free_quota, cfg)
        ),
        dest=cfg.bot_dir(BOT),
        offset=cfg.state / f'{BOT}.offset',
        chats=chats_from(env),
    )
    return TgCommands(api, spec, functools.partial(respond, cfg, env))


def main(env: Mapping[str, str] | None = None) -> int:
    """Build Settings once, warm CLIP, service queries forever."""
    mapping = os.environ if env is None else env
    cfg = load(mapping)
    if mapping.get('TG_TOKEN'):
        warm_embedder()  # resources at init, never mid-flight
    return run(BOT, build(cfg, mapping), cfg.logs)


if __name__ == '__main__':
    raise SystemExit(main())
