"""Discord DMs when missions complete (respects website notification prefs)."""

from __future__ import annotations

import logging
from typing import Any

from poke_pon_bot.services.missions import MissionProgressNotice, MissionService
from poke_pon_bot.services.notification_delivery import PREF_MISSIONS, schedule_notification

_LOG = logging.getLogger(__name__)


def schedule_mission_completion_dms(
    bot: Any,
    *,
    user_id: int,
    notices: list[MissionProgressNotice],
) -> None:
    """Fire-and-forget inbox row + optional DM for completed missions."""
    completed = [n for n in notices if n.completed]
    if not completed:
        return
    settings = getattr(bot, "settings", None)
    base = (getattr(settings, "web_frontend_url", None) or "").rstrip("/")
    missions_url = f"{base}/missions/" if base else None

    svc = MissionService()
    parts: list[str] = []
    for notice in completed:
        m = notice.mission
        parts.append(
            f"{svc.describe_mission_plain(m)} — claim {int(m.reward_crystals)} 💎"
        )
    body = "; ".join(parts)
    discord_parts: list[str] = []
    for notice in completed:
        m = notice.mission
        discord_parts.append(
            f"⭐ **Mission complete!** {svc.describe_mission_plain(m)}\n"
            f"Claim **{int(m.reward_crystals)}** 💎"
        )
    if missions_url:
        discord_parts.append(
            f"Open **{missions_url}** to claim on the website, or use **`/missions`** in Discord."
        )
    else:
        discord_parts.append("Use **`/missions`** in Discord to claim your reward.")

    schedule_notification(
        bot,
        user_id=int(user_id),
        kind="mission_ready",
        title="Mission ready to claim",
        body=body,
        href=missions_url,
        discord_pref=PREF_MISSIONS,
        discord_body="\n\n".join(discord_parts),
    )
