# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared state contract for the Aggregator mixins.

Each glue mixin (status, cats, stories, comod, users, commands) reads state
and calls peer methods off ``self`` that ``Aggregator`` provides. Rather than
each mixin re-declaring those names in its own ``TYPE_CHECKING`` block (four
hand-kept copies that can silently drift), they all inherit
``AggregatorProtocol``: one place that tells the type checker what ``self``
carries. At runtime it is an empty marker base, so it adds no behaviour and
no import of Telethon.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from telethon import TelegramClient

    from minions.aggregator import cats
    from minions.aggregator import comod
    from minions.aggregator import greeter
    from minions.aggregator import stories
    from minions.aggregator import users
    from minions.aggregator.models import Config
    from minions.aggregator.models import Consts
    from minions.aggregator.models import Group
    from minions.aggregator.models import Posted

    class AggregatorProtocol:
        """What every Aggregator mixin may read off ``self`` (type-only).

        Declared once here and inherited by each mixin; ``Aggregator`` itself
        provides the real attributes and implementations. Only the type
        checker sees this body -- at runtime the class below is empty.
        """

        # --- runtime collaborators ---
        client: TelegramClient
        config: Config
        consts: Consts
        state_path: Path
        cats: cats.CatBrain
        stories: stories.StoryBrain
        greeter: greeter.Greeter
        users: users.UserStore
        comod: comod.CabinetRoster
        _comod: comod.ComodParams

        # --- live state ---
        mode: str
        groups: list[Group]
        posted: list[Posted]
        rejected: set[str]
        _users_enabled: bool
        _cat_next_rescan: float
        _story_next_poll: float
        _rescan_sec: float
        _cat_tasks: set[asyncio.Task[None]]
        _story_tasks: set[asyncio.Task[None]]
        _thread_rescan_at: dict[int, float]
        _pending_views: list[stories.StoryView]

        # --- peer methods implemented on Aggregator or a sibling mixin ---
        def live_targets(self) -> tuple[int, ...]:
            """Return the active profile's post destinations."""

        async def _watch_post(self, target: int, post_id: int) -> None: ...

        async def _send_status(self, text: str) -> None: ...

        async def _chat_label(self, chat_id: int) -> str: ...

        def _pending_cat_line(
            self, entry: dict[str, object], now: float
        ) -> str: ...

        def _stories_line(self) -> str: ...

        def _stories_queue_lines(
            self, labels: dict[int, str]
        ) -> list[str]: ...

else:

    class AggregatorProtocol:
        """Runtime marker base for the mixins (empty; see the typed body)."""
