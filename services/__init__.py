"""Platform tier: HTTP/OpenAPI + MCP skins over the Phase 0 service core.

Separate from the austere kernel (PLATFORM.md, section 9): this package
depends on a web stack (FastAPI, MCP, boto3) and carries lighter
conventions. It imports the Step catalog but never the other way round.
"""
