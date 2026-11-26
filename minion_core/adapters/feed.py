# Copyright (C) 2026 Artem Herych. All rights reserved.
# Proprietary -- no use without the author's prior approval.
"""Public-feed boundary: read one small JSON document over HTTPS.

A sanctioned ``requests`` import site (REQ-ARC-002). No token, no
cookies, no write: a GET of a public address, bounded in wall time and in
size. A failure RAISES ``FeedUnreachableError`` naming the address and
the reason, so the caller -- a background worker -- logs and waits for
its next tick instead of reading "no feed" as "nothing to do".

The site sits behind Cloudflare, which turns away the stdlib's default
client name (403), so the client says who it is.
"""

from __future__ import annotations

TIMEOUT_SEC = 30.0
"""Wall-time bound on one read."""
LIMIT_BYTES = 1_000_000
"""A feed larger than this is refused rather than read into memory."""
USER_AGENT = 'minion2-feed/1'


class FeedUnreachableError(RuntimeError):
    """The feed could not be read; the message names the address and why."""


def read(url: str, timeout: float = TIMEOUT_SEC) -> str:
    """Return the body at ``url`` as text (UTF-8)."""
    import requests

    try:
        with requests.get(
            url,
            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
            timeout=timeout,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            body = resp.raw.read(LIMIT_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        msg = f'{url}: {type(exc).__name__}'
        raise FeedUnreachableError(msg) from None
    if len(body) > LIMIT_BYTES:
        msg = f'{url}: larger than {LIMIT_BYTES} bytes'
        raise FeedUnreachableError(msg)
    try:
        return body.decode('utf-8')
    except UnicodeDecodeError as exc:
        msg = f'{url}: not UTF-8 at byte {exc.start}'
        raise FeedUnreachableError(msg) from None
