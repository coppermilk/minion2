# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The shared state contract for the Userbot mixins.

Each glue mixin (status, cats, stories, comod, users, commands) reads state
and calls peer methods off ``self`` that ``Userbot`` provides. Rather than
each mixin re-declaring those names in its own ``TYPE_CHECKING`` block (four
hand-kept copies that can silently drift), they all inherit
``UserbotProtocol``: one place that tells the type checker what ``self``
carries. At runtime it is an empty marker base, so it adds no behaviour and
no import of Telethon.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from telethon import TelegramClient

    from minions.userbot.core.models import Config
    from minions.userbot.core.models import Consts
    from minions.userbot.core.models import Group
    from minions.userbot.core.models import Posted
    from minions.userbot.engines import cats
    from minions.userbot.engines import comod
    from minions.userbot.engines import greeter
    from minions.userbot.engines import stories
    from minions.userbot.engines import users

    class UserbotProtocol:
        """What every Userbot mixin may read off ``self`` (type-only).

        Declared once here and inherited by each mixin; ``Userbot`` itself
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
        _users_enrich: bool
        _users_store_text: bool
        _enrich_tasks: set[asyncio.Task[None]]
        _cat_next_rescan: float
        _story_next_poll: float
        _rescan_sec: float
        _cat_tasks: set[asyncio.Task[None]]
        _story_tasks: set[asyncio.Task[None]]
        _thread_rescan_at: dict[int, float]
        _pending_views: list[stories.StoryView]
        _modes: dict[str, str]  # service -> 'off' | 'test' | 'live'

        # --- peer methods implemented on Userbot or a sibling mixin ---
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

        # --- profile + status helpers the command dispatcher calls ---
        def _bul(self) -> str: ...

        def _dot(self, *, on: bool) -> str: ...

        def _build_profile(self) -> None: ...

        def _feature_enabled(self, name: str) -> bool: ...

        def _save_service_modes(self) -> None: ...

        async def set_service_mode(self, name: str, mode: str) -> None:
            """Set one service's mode (off/test/live) and rebuild."""

        async def start_profile(
            self, *, source_backfill: bool = True
        ) -> None:
            """Hydrate the active profile and start its loops."""

        async def stop_profile(self) -> None:
            """Cancel the active profile's timers and loops."""

        async def switch_mode(self, mode: str) -> None:
            """Switch the whole bot to live/test."""

        async def status_report(self) -> None:
            """Post the /status report."""

        # --- command handlers implemented on sibling mixins ---
        async def requeue_cats(self) -> None:
            """Rebuild the pending-cat queue (/requeue)."""

        async def answer_all_now(self) -> None:
            """Answer every pending commenter now (/catnow)."""

        async def users_report(self) -> None:
            """Post the users-DB summary (/users)."""

        async def stories_report(self) -> None:
            """Post the story-viewer log (/stories)."""

        async def cabinet_command(self, text: str) -> None:
            """Run a /comod cabinet command."""

        async def propiska_report(self) -> None:
            """Post the month's cabinet registry (/propiska_shkaf_month)."""

else:

    class UserbotProtocol:
        """Runtime marker base for the mixins (empty; see the typed body)."""
