# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Monolith bots: units that are not a file-service behind Telegram.

Ingest (inbox), chat commands (moderator, props), folder/cron work
(sort, week_clean) and the Windows-only pair (print, catch). One
directory per bot; no bot imports a sibling bot (REQ-ARC-001).
"""
