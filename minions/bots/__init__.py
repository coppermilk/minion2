"""Monolith bots: units that are not a file-service behind Telegram.

Ingest (inbox), chat commands (model_switch, props), folder/cron work
(sort, week_clean) and the Windows-only pair (print, catch). One
directory per bot; no bot imports a sibling bot (REQ-ARC-001).
"""
