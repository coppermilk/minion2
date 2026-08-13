# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Telethon userbot: render a donation shout-out on the /donate command.

A user-session app (not a bot token -- only a user account may send premium
emoji), separate from the aggregator: one Telethon session is one account, so a
second account is a second app. Run it with ``python -m minions.donate.main``.
"""
