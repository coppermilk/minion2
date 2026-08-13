# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Atomic web services: one container per Step, bytes in / bytes out.

Each subpackage is a self-contained minion -- its ``step.py`` owns the
model and the IP, its ``service.py`` serves that one Step over HTTP/MCP
(``python -m minions.svc.<name>.service``). No service imports a sibling
service or the Telegram transport (REQ-ARC-001): it knows only itself.
"""
