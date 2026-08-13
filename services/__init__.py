# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Atomic web services: HTTP/OpenAPI + MCP skins over one Step.

Separate from the austere kernel: this package depends on a web stack
(FastAPI, MCP) and carries lighter conventions. Bytes in, bytes out -- it
imports the Step catalog but never the other way round.
"""
