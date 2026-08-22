# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""The cabinet ("comod") commands, mixed into Aggregator.

Extracted from ``main``: the /comod and /propiska handlers plus their image
render + caption helpers. ``_ComodMixin`` is mixed into ``Aggregator`` with
method bodies unchanged, so they keep reading ``self`` state; it inherits
``AggregatorProtocol`` (base.py) so the type checker knows that state.
"""

from __future__ import annotations

import html
import logging
import random
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from minion_core.adapters import files
from minions.aggregator.core.base import AggregatorProtocol
from minions.aggregator.core.config import PACKAGE_DIR
from minions.aggregator.engines import comod

log = logging.getLogger('aggregator')


class _ComodMixin(AggregatorProtocol):
    """The cabinet commands, mixed into Aggregator (reads its state)."""

    async def cabinet_command(self, text: str) -> None:
        """Move a nick into the cabinet, evict one, or re-post the cabinet.

        ``/comod <nick> <amount>`` seats NICK on a shelf (refreshing the 30-day
        timer) and posts the rendered cabinet photo with the announcement;
        ``/comod kick <nick>`` evicts by hand and re-posts; a bare ``/comod``
        just re-posts the cabinet. Expired nicks are pruned by the roster on
        every write and read, so a shelf frees up ("s'ekhal") a month after
        its move-in with no extra step. The month's roster of who lives where
        is a separate command (``/propiska_shkaf_month``).
        """
        args = text.split()[1:]
        now = time.time()
        if args and args[0].lower() == 'kick':
            target = args[1].lstrip('@') if len(args) > 1 else ''
            if not target:
                hint = str(self._comod.templates.get('kick_hint', ''))
                await self.client.send_message(self._comod_chat(), hint)
                return
            self.comod.remove(target)
            log.info('comod: evicted %s', target)
        elif args:
            moved_in = args[0]
            amount = args[1] if len(args) > 1 else ''
            self.comod.add(moved_in, amount, now)
            log.info('comod: moved in %s (%s)', moved_in, amount or '-')
        await self._post_cabinet(now)

    async def _post_cabinet(self, now: float) -> None:
        """Render and post the cabinet; text fallback on a render failure.

        Always posts to the source chat (where the command was issued). When
        there are more active residents than shelves, only the TOP donors by
        amount are drawn on the picture.
        """
        active = self.comod.active(now)
        residents = comod.by_amount(active)[: self._comod.max_shelves]
        caption = self._cabinet_caption(residents)
        chat = self._comod_chat()
        image = self._render_cabinet(residents)
        n = len(residents)
        if image is not None:
            try:
                await self.client.send_file(
                    chat, str(image), caption=caption, parse_mode='html'
                )
            except Exception:  # noqa: BLE001 -- bad render falls back to text
                log.warning('comod: image send failed; posting as text')
            else:
                log.info('comod: posted cabinet (%d in) to %s', n, chat)
                return
        await self.client.send_message(
            chat, caption, parse_mode='html', link_preview=False
        )
        log.info('comod: posted cabinet text (%d in) to %s', n, chat)

    def _comod_chat(self) -> object:
        """Where the cabinet posts: the source chat, always (for now)."""
        return self.config.source

    def _render_cabinet(self, residents: list[tuple[str, str]]) -> Path | None:
        """Return the rendered cabinet image, or None if it cannot be made.

        None whenever no template photo is configured (or is missing) or the
        draw fails -- the caller then posts a plain-text roster instead.
        """
        template = self._comod_asset(self._comod.template_path)
        if template is None or not template.is_file():
            return None
        out = self.state_path.parent / 'comod_render.jpg'
        try:
            return files.render_cabinet(
                template,
                out,
                files.CabinetSpec(
                    # Biggest amount on the biggest shelf (area-ranked).
                    comod.assign_labels(residents, self._comod.slots),
                    list(self._comod.slots),
                    font_path=self._comod_font(self._comod.font_path),
                    cyrillic_font_path=self._comod_font(
                        self._comod.font_cyrillic_path
                    ),
                    ref_size=self._comod.ref_size,
                    base_size=self._comod.base_size,
                    amount_scale=self._comod.amount_scale,
                    text_color=self._comod.text_color,
                    shadow_color=self._comod.shadow_color,
                ),
            )
        except Exception:  # noqa: BLE001 -- any Pillow failure -> text roster
            log.warning('comod: render failed for %s', template)
            return None

    def _comod_asset(self, rel: str) -> Path | None:
        """Resolve a comod asset path; a relative one sits in the package.

        So 'assets/cabinet.jpg' and 'assets/fonts/Aleo.ttf' are found no matter
        the working directory (anchored on the package root, not this file's
        subpackage). Returns None for a blank path.
        """
        if not rel:
            return None
        path = Path(rel)
        return path if path.is_absolute() else PACKAGE_DIR / rel

    def _comod_font(self, rel: str) -> str:
        """Return a bundled font path as a string, or '' when unset/missing.

        Empty lets ``render_cabinet`` fall back to its system-font search.
        """
        path = self._comod_asset(rel)
        return str(path) if path is not None and path.is_file() else ''

    def _cabinet_caption(self, residents: list[tuple[str, str]]) -> str:
        """Return the photo caption: the announcement, or the empty note.

        Only the announcement (with its premium emoji and donation link); who
        lives on which shelf is shown on the picture, and the month's roster is
        the separate /propiska command.
        """
        tpl = self._comod.templates
        if not residents:
            return str(tpl.get('empty', ''))
        return comod.move_in_text(
            tpl,
            '',
            {
                'link': self._comod.donate_link,
                'amazon': self._comod.amazon_link,
            },
        )

    async def propiska_report(self) -> None:
        """Post the month's cabinet registry as text (/propiska_shkaf_month).

        One line per resident -- a random premium heart, the nick, and the
        move-in date -- sent as HTML so the hearts render as premium emoji.
        """
        tpl = self._comod.templates
        entries = self.comod.entries(time.time())
        chat = self._comod_chat()
        if not entries:
            await self.client.send_message(
                chat, str(tpl.get('propiska_empty', '')), parse_mode='html'
            )
            return
        line = str(tpl.get('propiska_line', '{heart} {nick} {date}'))
        rows = [
            line.format(
                heart=self._heart_html(),
                nick=html.escape(nick),
                date=self._move_in_date(at),
            )
            for nick, _amount, at in entries
        ]
        head = str(tpl.get('propiska_head', ''))
        body = '\n'.join(rows)
        text = f'{head}\n{body}' if head else body
        await self.client.send_message(
            chat, text, parse_mode='html', link_preview=False
        )
        log.info('comod: posted propiska (%d) to %s', len(entries), chat)

    def _heart_html(self) -> str:
        """Return a random heart: a premium <tg-emoji>, or its plain glyph."""
        hearts = self._comod.hearts
        if not hearts:
            return ''
        emoji_id, fallback = random.choice(hearts)  # noqa: S311 -- decoration
        if emoji_id:
            return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
        return fallback

    def _move_in_date(self, at: float) -> str:
        """Format a move-in epoch as a date in the persona's timezone."""
        tz = timezone(timedelta(hours=self._comod.tz_offset))
        return datetime.fromtimestamp(at, tz=tz).strftime('%d.%m.%Y')
