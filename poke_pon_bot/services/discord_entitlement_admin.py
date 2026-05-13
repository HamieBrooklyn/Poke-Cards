"""Discord monetization admin helpers (HTTP quirks not covered by high-level client APIs)."""

from __future__ import annotations

import logging

import discord
from discord.enums import EntitlementOwnerType
from discord.http import Route

_LOG = logging.getLogger(__name__)


async def create_test_user_entitlement(
    client: discord.Client,
    *,
    sku_id: int,
    owner_user_id: int,
) -> None:
    """Grant a **test** entitlement via ``POST /applications/.../entitlements``.

    Discord’s documented JSON body uses **string** snowflakes for ``sku_id`` and ``owner_id``.
    discord.py’s :meth:`discord.Client.create_entitlement` currently may serialize those as
    JSON numbers, which some API versions reject with **400 Invalid SKU**.
    """
    if client.application_id is None:
        msg = "Bot has no application_id yet"
        raise RuntimeError(msg)
    route = Route(
        "POST",
        "/applications/{application_id}/entitlements",
        application_id=client.application_id,
    )
    payload = {
        "sku_id": str(sku_id),
        "owner_id": str(owner_user_id),
        "owner_type": EntitlementOwnerType.user.value,
    }
    _LOG.debug("create_test_entitlement payload=%s", payload)
    await client.http.request(route, json=payload)


async def consume_entitlement(client: discord.Client, *, entitlement_id: int) -> None:
    """Mark a consumable entitlement as **used** so the user can buy it again.

    Discord won't grant a second purchase of the same SKU until the previous entitlement is
    consumed via ``POST /applications/{app}/entitlements/{ent}/consume``. discord.py doesn't
    expose this endpoint as of 2.5.x, so we hit the route directly.
    """
    if client.application_id is None:
        msg = "Bot has no application_id yet"
        raise RuntimeError(msg)
    route = Route(
        "POST",
        "/applications/{application_id}/entitlements/{entitlement_id}/consume",
        application_id=client.application_id,
        entitlement_id=int(entitlement_id),
    )
    await client.http.request(route)
