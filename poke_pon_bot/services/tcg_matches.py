"""Atomic TCG lobby transitions and viewer-specific transport projections."""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.tcg_card import TcgCardDefinition
from poke_pon_bot.models.tcg_match import TcgCommand, TcgLobby, TcgSavedDeck
from poke_pon_bot.services import tcg_catalog, tcg_engine

GRACE_SECONDS = 180


class TcgError(ValueError):
    def __init__(self, message: str, status: int = 400, details: list | None = None):
        super().__init__(message)
        self.status = status
        self.details = details or []


def _settings(source: Any, mixed: Any) -> dict:
    if source not in ("global", "owned") or not isinstance(mixed, bool):
        raise TcgError("Choose Global or Owned and a valid Mixed setting.")
    if source == "global" and mixed:
        raise TcgError("Mixed sharing is available with Owned decks.")
    return {"source": source, "mixed": mixed}


def _entries(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 60:
        raise TcgError("A deck must contain at most 60 different entries.")
    result: dict[str, int] = {}
    for entry in value:
        if not isinstance(entry, dict):
            raise TcgError("Invalid deck entry.")
        card_id, n = entry.get("card_id"), entry.get("quantity")
        if not isinstance(card_id, str) or not 1 <= len(card_id) <= 128:
            raise TcgError("Every deck entry needs a catalog card identifier.")
        if type(n) is not int or not 1 <= n <= 60:
            raise TcgError("Card quantities must be whole numbers from 1 to 60.")
        result[card_id] = result.get(card_id, 0) + n
    if sum(result.values()) > 60:
        raise TcgError("A deck may contain at most 60 cards.")
    return [{"card_id": k, "quantity": v} for k, v in sorted(result.items())]


def _member(lobby: TcgLobby, uid: int) -> None:
    if uid not in (lobby.host_id, lobby.guest_id):
        raise TcgError("This lobby belongs to other players.", 403)


def _epoch(dt: datetime | None) -> float:
    if dt is None:
        return 0
    return dt.replace(tzinfo=UTC).timestamp() if dt.tzinfo is None else dt.timestamp()


class TcgService:
    def __init__(self, session_factory):
        self.sessions = session_factory
        self._catalog: dict[str, dict] | None = None
        self._catalog_at = 0.0
        self.started_at = time.time()

    async def definitions(self, db, *, fresh: bool = False) -> dict[str, dict]:
        if not fresh and self._catalog is not None and time.monotonic() - self._catalog_at < 60:
            return self._catalog
        rows = (await db.execute(select(Card, TcgCardDefinition).outerjoin(
            TcgCardDefinition, TcgCardDefinition.tcg_card_id == Card.tcg_card_id))).all()
        result = {}
        for card, definition in rows:
            result[card.tcg_card_id] = tcg_catalog.normalize_card(card, definition.data if definition else None)
        self._catalog = result
        self._catalog_at = time.monotonic()
        return result

    async def _owned(self, db, uid: int | None) -> dict[str, int]:
        if uid is None:
            return {}
        rows = (await db.execute(select(Card.tcg_card_id, func.count(UserCardInstance.id))
            .join(UserCardInstance, UserCardInstance.card_id == Card.id)
            .where(UserCardInstance.discord_user_id == uid).group_by(Card.tcg_card_id))).all()
        return dict(rows)

    async def availability(self, db, lobby: TcgLobby, uid: int):
        if lobby.status == "closed":
            raise TcgError("This table is closed. Create a new lobby to choose a card pool.", 409)
        if lobby.status in {"active", "finished"}:
            if lobby.data["source"] == "global":
                return None, {}, {}
            # Historical tables must not become a feed of future collection
            # changes. Older games have only the selected-deck eligibility map.
            snapshot = lobby.data.get("pool_snapshot", {}).get(str(uid))
            if snapshot is not None:
                return snapshot["available"], snapshot["mine"], snapshot["theirs"]
            available = lobby.data.get("eligibility_snapshot", {}).get(str(uid), {})
            return available, available, {}
        mine = await self._owned(db, uid)
        other = lobby.guest_id if uid == lobby.host_id else lobby.host_id
        theirs = await self._owned(db, other) if lobby.data["mixed"] else {}
        if lobby.data["source"] == "global":
            return None, mine, theirs
        if lobby.data["mixed"] and lobby.guest_id is None:
            raise TcgError("Invite a second player before building a Mixed deck.")
        quantities = dict(mine)
        for key, count in theirs.items():
            quantities[key] = quantities.get(key, 0) + count
        return quantities, mine, theirs

    async def _get(self, db, lobby_id: str, uid: int) -> TcgLobby:
        lobby = await db.get(TcgLobby, lobby_id)
        if lobby is None:
            raise TcgError("Lobby not found.", 404)
        _member(lobby, uid)
        return lobby

    def enrich_entries(self, entries):
        return [{**e, "card": (self._catalog or {}).get(e["card_id"])} for e in entries]

    def project(self, lobby: TcgLobby, uid: int) -> dict:
        _member(lobby, uid)
        data = lobby.data
        players = []
        for player in data["players"]:
            pid = player["id"]
            seen = lobby.host_seen if int(pid) == lobby.host_id else lobby.guest_seen
            last_seen = max(_epoch(seen), self.started_at)
            players.append({"id": pid, "name": player["name"],
                "ready": pid in data["ready"],
                "deck_count": sum(x["quantity"] for x in data["decks"].get(pid, [])),
                "connected": time.time() - _epoch(seen) < 45,
                "disconnect_claim_in": max(0, int(GRACE_SECONDS - (time.time() - last_seen)))})
        return {"lobby": {"id": lobby.id, "code": lobby.code, "status": lobby.status,
            "version": lobby.version, "host_id": str(lobby.host_id),
            "source": data["source"], "mixed": data["mixed"], "players": players,
            "own_deck": self.enrich_entries(data["decks"].get(str(uid), [])),
            "disconnect_grace_seconds": GRACE_SECONDS,
            "format": "Casual all-catalog", "rules_version": getattr(tcg_engine, "RULES_VERSION", "2026-02")},
            "game": tcg_engine.project_state(data["game"], str(uid)) if data.get("game") else None}

    async def touch(self, lobby_id: str, uid: int):
        async with self.sessions() as db:
            lobby = await self._get(db, lobby_id, uid)
            field = "host_seen" if lobby.host_id == uid else "guest_seen"
            await db.execute(update(TcgLobby).where(TcgLobby.id == lobby_id).values(**{field: datetime.now(UTC)}))
            await db.commit()

    async def get(self, lobby_id: str, uid: int) -> dict:
        async with self.sessions() as db:
            lobby = await self._get(db, lobby_id, uid)
            if self._catalog is None:
                await self.definitions(db)
            return self.project(lobby, uid)

    async def list(self, uid: int) -> list:
        async with self.sessions() as db:
            rows = (await db.scalars(select(TcgLobby).where(or_(TcgLobby.host_id == uid, TcgLobby.guest_id == uid))
                .order_by(TcgLobby.updated_at.desc()).limit(30))).all()
            return [self.project(row, uid)["lobby"] for row in rows]

    async def create(self, uid: int, name: str, source="global", mixed=False) -> dict:
        cfg = _settings(source, mixed)
        async with self.sessions() as db:
            n = await db.scalar(select(func.count()).select_from(TcgLobby).where(
                TcgLobby.host_id == uid, TcgLobby.status.in_(["waiting", "active"])))
            if n >= 10:
                raise TcgError("Close an existing lobby before creating another.", 429)
            lobby = TcgLobby(id=uuid.uuid4().hex, code="".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)),
                host_id=uid, status="waiting", version=0, host_seen=datetime.now(UTC),
                data={**cfg, "players": [{"id": str(uid), "name": name[:80]}], "decks": {}, "ready": [], "game": None})
            db.add(lobby)
            await db.commit()
            return self.project(lobby, uid)

    async def join(self, uid: int, name: str, code: str) -> dict:
        async with self.sessions() as db:
            lobby = await db.scalar(select(TcgLobby).where(TcgLobby.code == str(code).strip().upper()))
            if lobby is None:
                raise TcgError("No lobby matches that invite code.", 404)
            if uid in (lobby.host_id, lobby.guest_id):
                return self.project(lobby, uid)
            if lobby.guest_id is not None or lobby.status != "waiting":
                raise TcgError("This lobby already has two players or is closed.", 409)
            data = copy.deepcopy(lobby.data)
            data["players"].append({"id": str(uid), "name": name[:80]})
            data["ready"] = []
            result = await db.execute(update(TcgLobby).where(TcgLobby.id == lobby.id, TcgLobby.version == lobby.version,
                TcgLobby.guest_id.is_(None)).values(guest_id=uid, guest_seen=datetime.now(UTC), data=data, version=lobby.version + 1))
            if result.rowcount != 1:
                raise TcgError("Another player joined first. Refresh the lobby.", 409)
            await db.commit()
            await db.refresh(lobby)
            return self.project(lobby, uid)

    async def catalog(self, uid: int, lobby_id: str | None, query="", supertype="", only_supported=False, page=1, ids=None) -> dict:
        async with self.sessions() as db:
            definitions = await self.definitions(db)
            available, mine, theirs = None, {}, {}
            if lobby_id:
                lobby = await self._get(db, lobby_id, uid)
                available, mine, theirs = await self.availability(db, lobby, uid)
            pool = [v for k, v in definitions.items() if available is None or available.get(k, 0) > 0]
            coverage = {"total": len(pool), "supported": sum(bool(v["supported"]) for v in pool)}
            coverage["unsupported"] = coverage["total"] - coverage["supported"]
            q = query.strip().casefold()
            filtered = [v for v in pool if (not q or q in (v["name"] + " " + v["id"]).casefold())
                and (not supertype or v["supertype"] == supertype) and (not only_supported or v["supported"])
                and (not ids or v["id"] in ids)]
            filtered.sort(key=lambda c: (not c["supported"], c["name"], c["id"]))
            page = max(1, min(page, 10000))
            cards = []
            for card in filtered[(page - 1) * 48:page * 48]:
                k = card["id"]
                ownership = "global" if available is None else ("both" if mine.get(k) and theirs.get(k) else "yours" if mine.get(k) else "opponents")
                count = available.get(k, 0) if available is not None else None
                cards.append({**card, "image_url": card.get("image_small") or card.get("image_large"),
                    "unsupported_reason": "; ".join(card.get("unsupported_reasons", [])),
                    "available": count, "available_quantity": count,
                    "owned": mine.get(k, 0), "opponent_owned": theirs.get(k, 0), "ownership": ownership})
            return {"cards": cards, "total": len(filtered), "page": page, "page_size": 48, "coverage": coverage}

    async def starter(self, uid: int, lobby_id: str | None):
        async with self.sessions() as db:
            definitions = await self.definitions(db)
            available = None
            if lobby_id:
                lobby = await self._get(db, lobby_id, uid)
                available, _, _ = await self.availability(db, lobby, uid)
            entries = tcg_catalog.starter_deck(definitions, available)
            if not entries:
                raise TcgError("This card pool cannot yet supply a supported 60-card starter deck.")
            errors = tcg_catalog.validate_deck(entries, definitions, available)
            if errors:
                raise TcgError("A supported starter is unavailable for this pool.", details=errors)
            return {"entries": entries}

    async def decks(self, uid: int) -> list:
        async with self.sessions() as db:
            await self.definitions(db)
            rows = (await db.scalars(select(TcgSavedDeck).where(TcgSavedDeck.owner_id == uid)
                .order_by(TcgSavedDeck.updated_at.desc()).limit(100))).all()
            return [{"id": row.id, "name": row.name, "entries": self.enrich_entries(row.entries)} for row in rows]

    async def save_deck(self, uid: int, body: dict) -> dict:
        entries = _entries(body.get("entries", body.get("cards")))
        name = str(body.get("name") or "Untitled deck").strip()[:80]
        async with self.sessions() as db:
            definitions = await self.definitions(db)
            unknown = [e["card_id"] for e in entries if e["card_id"] not in definitions]
            if unknown:
                raise TcgError("A deck entry is not in the catalog.", details=unknown)
            row = await db.get(TcgSavedDeck, str(body["id"])) if body.get("id") else None
            if body.get("id") and (row is None or row.owner_id != uid):
                raise TcgError("Saved deck not found.", 404)
            if row is None:
                count = await db.scalar(select(func.count()).select_from(TcgSavedDeck).where(TcgSavedDeck.owner_id == uid))
                if count >= 100:
                    raise TcgError("You have reached the 100 saved deck limit.", 429)
                row = TcgSavedDeck(id=uuid.uuid4().hex, owner_id=uid, name=name, entries=entries)
                db.add(row)
            else:
                row.name, row.entries = name, entries
            await db.commit()
            return {"id": row.id, "name": row.name, "entries": row.entries,
                "validation_errors": tcg_catalog.validate_deck(entries, definitions, None)}

    async def command(self, lobby_id: str, uid: int, body: dict) -> dict:
        command_id, expected, action = body.get("command_id"), body.get("version"), body.get("action")
        if not isinstance(command_id, str) or not 8 <= len(command_id) <= 80:
            raise TcgError("Every command needs a unique command_id (8–80 characters).")
        if type(expected) is not int or expected < 0 or not isinstance(action, dict):
            raise TcgError("Every command needs a version and action object.")
        digest = hashlib.sha256(json.dumps(action, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            async with self.sessions() as db:
                lobby = await self._get(db, lobby_id, uid)
                prior = await db.scalar(select(TcgCommand).where(TcgCommand.lobby_id == lobby_id,
                    TcgCommand.actor_id == uid, TcgCommand.command_id == command_id))
                if prior:
                    if prior.digest != digest:
                        raise TcgError("That command_id was already used for a different action.", 409)
                    return self.project(lobby, uid)
                if lobby.version != expected:
                    raise TcgError("The lobby changed. Refresh before acting again.", 409)
                # Acquire the write transaction before reading inventory. In
                # SQLite this also closes the read/modify window with trades;
                # other backends serialize concurrent commands on this row.
                lock = await db.execute(update(TcgLobby).where(TcgLobby.id == lobby_id,
                    TcgLobby.version == expected).values(version=expected))
                if lock.rowcount != 1:
                    raise TcgError("Another action was committed first. Refresh and retry.", 409)
                # Presence can change without a gameplay version increment.
                # Re-read under the write lock before deciding a disconnect claim.
                await db.refresh(lobby)
                data = copy.deepcopy(lobby.data)
                kind, pid = action.get("type"), str(uid)
                status, guest_id = lobby.status, lobby.guest_id
                if kind == "leave":
                    if status == "active":
                        raise TcgError("Surrender before leaving an active match.")
                    if uid == lobby.host_id:
                        status = "closed"
                        data["ready"] = []
                    else:
                        # End the shared room instead of silently removing a participant
                        # from the history of their finished game.
                        status = "closed"
                        data["ready"] = []
                elif kind == "rematch":
                    if status != "finished" or guest_id is None:
                        raise TcgError("Finish the match before requesting a rematch.")
                    data["game"], data["ready"], status = None, [], "waiting"
                elif kind in ("settings", "deck", "ready", "start"):
                    if status != "waiting":
                        raise TcgError("Decks and lobby settings are locked during a match.")
                    if kind == "settings":
                        if uid != lobby.host_id:
                            raise TcgError("Only the host can change lobby settings.", 403)
                        data.update(_settings(action.get("source"), action.get("mixed", False)))
                        data["decks"], data["ready"] = {}, []
                    elif kind == "deck":
                        entries = _entries(action.get("entries", action.get("cards")))
                        # Draft decks may be incomplete; never ready/start them until legal.
                        definitions = await self.definitions(db)
                        available, _, _ = await self.availability(db, lobby, uid)
                        for entry in entries:
                            card = definitions.get(entry["card_id"])
                            if not card or not card["supported"]:
                                reasons = card.get("unsupported_reasons", []) if card else []
                                raise TcgError("This deck contains an unsupported card.", details=reasons)
                            if available is not None and entry["quantity"] > available.get(entry["card_id"], 0):
                                raise TcgError(f"Not enough available copies of {card['name']}.")
                        data["decks"][pid], data["ready"] = entries, []
                    elif kind == "ready":
                        if type(action.get("ready")) is not bool:
                            raise TcgError("ready must be true or false.")
                        if action["ready"]:
                            definitions = await self.definitions(db)
                            available, _, _ = await self.availability(db, lobby, uid)
                            errors = tcg_catalog.validate_deck(data["decks"].get(pid, []), definitions, available)
                            if errors:
                                raise TcgError("Your deck is not ready.", details=errors)
                            if pid not in data["ready"]:
                                data["ready"].append(pid)
                        else:
                            data["ready"] = [p for p in data["ready"] if p != pid]
                    elif kind == "start":
                        if uid != lobby.host_id:
                            raise TcgError("Only the host can start the match.", 403)
                        ids = [p["id"] for p in data["players"]]
                        if guest_id is None or set(data["ready"]) != set(ids):
                            raise TcgError("Both players must choose legal decks and ready up.")
                        # All ownership reads, definition snapshots and the CAS state write
                        # take place in the same transaction as the start transition.
                        definitions = await self.definitions(db, fresh=True)
                        expanded, ownership, pools = {}, {}, {}
                        for p in ids:
                            available, mine, theirs = await self.availability(db, lobby, int(p))
                            entries = data["decks"].get(p, [])
                            errors = tcg_catalog.validate_deck(entries, definitions, available)
                            if errors:
                                raise TcgError("A deck changed eligibility. Rebuild it before starting.", details=errors)
                            expanded[p] = [copy.deepcopy(definitions[e["card_id"]]) for e in entries for _ in range(e["quantity"])]
                            ownership[p] = {e["card_id"]: available.get(e["card_id"], 0) for e in entries} if available is not None else None
                            if available is not None:
                                pools[p] = {"available": available, "mine": mine, "theirs": theirs}
                        data["eligibility_snapshot"] = ownership
                        data["pool_snapshot"] = pools
                        data["game"] = tcg_engine.new_game(expanded, ids)
                        status = "active"
                elif kind == "claim_disconnect":
                    if status != "active" or guest_id is None:
                        raise TcgError("No active opponent to claim a disconnect against.")
                    other = guest_id if uid == lobby.host_id else lobby.host_id
                    seen = lobby.guest_seen if uid == lobby.host_id else lobby.host_seen
                    if time.time() - max(_epoch(seen), self.started_at) < GRACE_SECONDS:
                        raise TcgError("The opponent's reconnect grace period has not expired.")
                    data["game"] = tcg_engine.apply_action(data["game"], str(other), {"type": "surrender"})
                    status = "finished"
                else:
                    if status != "active" or not data.get("game"):
                        raise TcgError("Start a match before playing a card.")
                    data["game"] = tcg_engine.apply_action(data["game"], pid, action)
                    if data["game"].get("phase") == "finished":
                        status = "finished"
                values = {"data": data, "status": status, "guest_id": guest_id, "version": expected + 1}
                values["host_seen" if uid == lobby.host_id else "guest_seen"] = datetime.now(UTC)
                result = await db.execute(update(TcgLobby).where(TcgLobby.id == lobby_id, TcgLobby.version == expected).values(**values))
                if result.rowcount != 1:
                    raise TcgError("Another action was committed first. Refresh and retry.", 409)
                db.add(TcgCommand(lobby_id=lobby_id, actor_id=uid, command_id=command_id, digest=digest,
                    version=expected + 1, action=action, snapshot={"status": status, "data": data}))
                await db.commit()
                await db.refresh(lobby)
                return self.project(lobby, uid)
        except tcg_engine.RuleError as exc:
            raise TcgError(str(exc)) from exc
        except (IntegrityError, OperationalError) as exc:
            # A competing SQLite transaction/duplicate receipt must never partially
            # apply a move; the context manager rolls it back in full.
            raise TcgError("Another action changed the match. Refresh and retry.", 409) from exc
