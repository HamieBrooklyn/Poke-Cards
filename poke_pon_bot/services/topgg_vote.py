"""Top.gg v1 API: verify a Discord user has voted before granting rewards."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

_LOG = logging.getLogger(__name__)

TOPGG_V1_VOTES_URL = "https://top.gg/api/v1/projects/@me/votes"


@dataclass(frozen=True)
class TopggVoteStatus:
    """Active vote window from Top.gg (user has voted; ``expires_at`` is when they may vote again)."""

    created_at: datetime
    expires_at: datetime


class TopggAuthError(Exception):
    """Top.gg returned 401 (bad or legacy token)."""


class TopggRateLimitError(Exception):
    """Top.gg returned 429."""


def parse_topgg_iso8601(raw: str) -> datetime:
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def fetch_active_discord_vote(bearer_token: str, discord_user_id: int) -> TopggVoteStatus | None:
    """
    Returns vote info if the user has an **active** vote (now < ``expires_at``).

    Uses ``GET /projects/@me/votes/:id?source=discord`` (Top.gg v1).
    Returns ``None`` if the user did not vote or the vote window expired (404 or past ``expires_at``).
    """
    token = bearer_token.strip()
    if not token:
        return None
    url = f"{TOPGG_V1_VOTES_URL}/{int(discord_user_id)}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, headers=headers, params={"source": "discord"})
    except httpx.HTTPError:
        _LOG.exception("Top.gg vote check HTTP error for user %s", discord_user_id)
        raise

    if response.status_code == 404:
        return None
    if response.status_code == 401:
        _LOG.error("Top.gg API returned 401 — check TOPGG_API_TOKEN (Bearer v1 token).")
        raise TopggAuthError
    if response.status_code == 429:
        _LOG.warning("Top.gg rate limited vote check for user %s", discord_user_id)
        raise TopggRateLimitError
    response.raise_for_status()

    data = response.json()
    ca = data.get("created_at")
    ex = data.get("expires_at")
    if not isinstance(ca, str) or not isinstance(ex, str):
        _LOG.warning("Top.gg vote response missing timestamps: %s", data)
        return None

    created_at = parse_topgg_iso8601(ca)
    expires_at = parse_topgg_iso8601(ex)
    now = datetime.now(UTC)
    if now >= expires_at:
        return None
    return TopggVoteStatus(created_at=created_at, expires_at=expires_at)
