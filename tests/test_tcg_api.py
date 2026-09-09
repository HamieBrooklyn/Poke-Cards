"""Two-user HTTP/WebSocket integration, pool snapshots, races and recovery."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from poke_pon_bot.db.base import Base
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.tcg_card import TcgCardDefinition
from poke_pon_bot.models.tcg_match import TcgLobby, TcgCommand, TcgSavedDeck
from poke_pon_bot.web.tcg_api import register_tcg_api
from poke_pon_bot.web.sessions import encode_session
from poke_pon_bot.services.tcg_matches import TcgService


TABLES = [RarityClass.__table__, Card.__table__, UserCardInstance.__table__, TcgCardDefinition.__table__,
          TcgLobby.__table__, TcgCommand.__table__, TcgSavedDeck.__table__]


def raw_cards():
    return [{"id": f"fixture-{i}", "name": f"Testmon {i}", "supertype": "Pokémon", "subtypes": ["Basic"],
             "hp": "60", "types": ["Lightning"], "attacks": [{"name": "Strike", "damage": "30", "cost": []}]} for i in range(4)] + [
            {"id": "fixture-energy", "name": "Lightning Energy", "supertype": "Energy", "subtypes": ["Basic"]}]


async def seed(factory, cards=None):
    async with factory() as db:
        db.add(RarityClass(id=1, code="common", display_name="Common", sort_order=0))
        await db.flush()
        for i, raw in enumerate(cards or raw_cards(), 1):
            db.add(Card(id=i, tcg_card_id=raw["id"], name=raw["name"], supertype=raw["supertype"],
                        tcg_subtypes=raw["subtypes"], hp=raw.get("hp"), tcg_types=raw.get("types"), attacks=raw.get("attacks"),
                        set_code="fixture", set_name="Test cards", collector_number=str(i),
                        image_small_url=(raw.get("images") or {}).get("small", ""), image_large_url=(raw.get("images") or {}).get("large", ""), rarity_class_id=1))
            await db.flush()
            db.add(TcgCardDefinition(tcg_card_id=raw["id"], data=raw, data_version="test-only"))
        await db.commit()


@pytest_asyncio.fixture
async def api(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "tcg-test.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    await seed(factory)
    settings = SimpleNamespace(pokepon_runtime="staging", web_session_secret="tcg-test-only-secret",
                               web_session_ttl_seconds=3600, web_allowed_origins=("https://test.pokepon.invalid",))
    app = web.Application()
    register_tcg_api(app, bot=SimpleNamespace(async_session_factory=factory), settings=settings)
    client = TestClient(TestServer(app))
    await client.start_server()
    headers = {i: {"Authorization": "Bearer " + encode_session(settings.web_session_secret, user_id=i,
             username=f"Player {i}", global_name=None, avatar_url=None)} for i in (1, 2, 3)}
    yield SimpleNamespace(client=client, factory=factory, headers=headers, app=app, settings=settings)
    await client.close()
    await engine.dispose()


async def post(api, path, uid, body, expected=200):
    response = await api.client.post(path, headers=api.headers[uid], json=body)
    payload = await response.json()
    assert response.status == expected, payload
    return payload


async def command(api, state, uid, action, expected=200, command_id=None):
    return await post(api, f"/api/tcg/lobbies/{state['lobby']['id']}/commands", uid,
                      {"command_id": command_id or uuid4().hex, "version": state["lobby"]["version"], "action": action}, expected)


async def lobby(api, source="global", mixed=False):
    state = await post(api, "/api/tcg/lobbies", 1, {"source": source, "mixed": mixed}, 201)
    return await post(api, "/api/tcg/lobbies/join", 2, {"code": state["lobby"]["code"]})


async def ready_game(api):
    state = await lobby(api)
    entries = [{"card_id": f"fixture-{i}", "quantity": 4} for i in range(4)] + [{"card_id": "fixture-energy", "quantity": 44}]
    for uid in (1, 2):
        state = await command(api, state, uid, {"type": "deck", "entries": entries})
    for uid in (1, 2):
        state = await command(api, state, uid, {"type": "ready", "ready": True})
    return await command(api, state, 1, {"type": "start"})


@pytest.mark.asyncio
async def test_auth_origin_gate_third_player_and_private_saved_decks(api):
    assert (await api.client.get("/api/tcg/me")).status == 401
    headers = {**api.headers[1], "Origin": "https://evil.invalid"}
    assert (await api.client.post("/api/tcg/lobbies", headers=headers, json={})).status == 403
    state = await lobby(api)
    await post(api, "/api/tcg/lobbies/join", 3, {"code": state["lobby"]["code"]}, 409)
    assert (await api.client.get(f"/api/tcg/lobbies/{state['lobby']['id']}", headers=api.headers[3])).status == 403
    saved = await post(api, "/api/tcg/decks", 1, {"name": "Draft", "entries": [{"card_id": "fixture-0", "quantity": 1}]})
    await post(api, "/api/tcg/decks", 2, {"id": saved["id"], "name": "Steal", "entries": []}, 404)
    disabled = web.Application()
    settings = deepcopy(api.settings)
    settings.pokepon_runtime = "production"
    register_tcg_api(disabled, bot=SimpleNamespace(async_session_factory=api.factory), settings=settings)
    assert not list(disabled.router.routes())


@pytest.mark.asyncio
async def test_ready_settings_idempotence_and_competing_tabs(api):
    state = await lobby(api)
    old = deepcopy(state)
    cmd_id = uuid4().hex
    action = {"type": "deck", "entries": [{"card_id": "fixture-0", "quantity": 1}]}
    state = await command(api, state, 1, action, command_id=cmd_id)
    repeated = await command(api, old, 1, action, command_id=cmd_id)
    assert repeated["lobby"]["version"] == state["lobby"]["version"]
    await command(api, old, 1, {"type": "leave"}, command_id=cmd_id, expected=409)
    await command(api, old, 2, {"type": "ready", "ready": True}, expected=409)
    await command(api, state, 1, {"type": "ready", "ready": True}, expected=400)
    state = await command(api, state, 1, {"type": "settings", "source": "owned", "mixed": True})
    assert state["lobby"]["own_deck"] == [] and not any(p["ready"] for p in state["lobby"]["players"])
    # Both writes name the same version; exactly one can commit.
    lid, version = state["lobby"]["id"], state["lobby"]["version"]
    async def submit():
        return await api.client.post(f"/api/tcg/lobbies/{lid}/commands", headers=api.headers[1], json={
            "command_id": uuid4().hex, "version": version, "action": {"type": "settings", "source": "global", "mixed": False}})
    responses = await asyncio.gather(submit(), submit())
    assert sorted(r.status for r in responses) == [200, 409]


@pytest.mark.asyncio
async def test_mixed_quantities_and_ownership_snapshot(api):
    async with api.factory() as db:
        # Each owns two of every Basic. Only player 2 owns Energy.
        for uid in (1, 2):
            for i in range(4):
                for _ in range(2):
                    db.add(UserCardInstance(discord_user_id=uid, card_id=i+1, public_id=uuid4().hex, source="test"))
        for _ in range(44):
            db.add(UserCardInstance(discord_user_id=2, card_id=5, public_id=uuid4().hex, source="test"))
        await db.commit()
    state = await lobby(api, "owned", True)
    lid = state["lobby"]["id"]
    catalog = await (await api.client.get(f"/api/tcg/catalog?lobby_id={lid}", headers=api.headers[1])).json()
    assert next(c for c in catalog["cards"] if c["id"] == "fixture-energy")["ownership"] == "opponents"
    entries = [{"card_id": f"fixture-{i}", "quantity": 4} for i in range(4)] + [{"card_id": "fixture-energy", "quantity": 44}]
    for uid in (1, 2):
        state = await command(api, state, uid, {"type": "deck", "entries": entries})
    for uid in (1, 2):
        state = await command(api, state, uid, {"type": "ready", "ready": True})
    state = await command(api, state, 1, {"type": "start"})
    async with api.factory() as db:
        match = await db.get(TcgLobby, lid)
        snapshot = deepcopy(match.data["game"])
        inst = await db.scalar(select(UserCardInstance).where(UserCardInstance.discord_user_id == 2))
        inst.discord_user_id = 3
        await db.commit()
    restored = TcgService(api.factory)
    view = await restored.get(lid, 1)
    assert view["game"]["phase"] == "choose_start"
    async with api.factory() as db:
        match = await db.get(TcgLobby, lid)
        assert match.data["game"] == snapshot
        assert match.data["eligibility_snapshot"]["1"]["fixture-energy"] == 44
    # A historical match must not expose later changes to the other collection.
    later = await (await api.client.get(f"/api/tcg/catalog?lobby_id={lid}", headers=api.headers[1])).json()
    before_cards = {c["id"]: (c["owned"], c["opponent_owned"]) for c in catalog["cards"]}
    assert {c["id"]: (c["owned"], c["opponent_owned"]) for c in later["cards"]} == before_cards
    state = await command(api, state, 1, {"type": "surrender"})
    state = await command(api, state, 1, {"type": "rematch"})
    latest = await (await api.client.get(f"/api/tcg/catalog?lobby_id={lid}", headers=api.headers[1])).json()
    assert {c["id"]: (c["owned"], c["opponent_owned"]) for c in latest["cards"]} != before_cards
    await command(api, state, 2, {"type": "leave"})
    assert (await api.client.get(f"/api/tcg/catalog?lobby_id={lid}", headers=api.headers[1])).status == 409
    assert (await api.client.get(f"/api/tcg/starter-deck?lobby_id={lid}", headers=api.headers[1])).status == 409


@pytest.mark.asyncio
async def test_chunked_body_limit_cannot_be_bypassed_without_content_length(api):
    async def chunks():
        yield b'{"name":"'
        for _ in range(5):
            yield b'x' * 8192
        yield b'"}'
    response = await api.client.post("/api/tcg/decks", headers=api.headers[1], data=chunks())
    assert response.status == 413
    assert "too large" in (await response.json())["error"]


@pytest.mark.asyncio
async def test_full_match_websocket_privacy_reconnect_and_server_recovery(api):
    state = await ready_game(api)
    lid = state["lobby"]["id"]
    ws1 = await api.client.ws_connect(f"/api/tcg/lobbies/{lid}/ws", headers=api.headers[1])
    ws2 = await api.client.ws_connect(f"/api/tcg/lobbies/{lid}/ws", headers=api.headers[2])
    assert (await ws1.receive_json())["lobby"]["id"] == lid
    await ws2.receive_json()
    state = await command(api, state, int(state["game"]["coin_winner"]), {"type": "choose_start", "first_player": "1"})
    a, b = await ws1.receive_json(), await ws2.receive_json()
    assert isinstance(a["game"]["board"]["1"]["hand"], list)
    assert isinstance(b["game"]["board"]["1"]["hand"], dict)
    hidden = [c["uid"] for c in a["game"]["board"]["1"]["hand"]]
    assert all(uid not in json.dumps(b) for uid in hidden)
    await ws1.close()
    await ws2.close()
    for _ in range(400):
        if state["game"]["phase"] == "finished":
            break
        game = state["game"]
        if game.get("pending_choice"):
            uid = int(game["pending_choice"]["player"])
        elif game["phase"] == "setup":
            uid = next(int(p) for p in game["players"] if not game["board"][p]["setup_ready"])
        else:
            uid = int(game["turn_player"])
        # A fresh service object simulates recovery from persisted state.
        view = await TcgService(api.factory).get(lid, uid)
        choice = view["game"].get("pending_choice")
        if choice:
            action = {"type": "choose", "choice_id": choice["id"], "selected": [o["value"] for o in choice["options"][:choice["min"]]]}
        else:
            actions = [a["action"] for a in view["game"]["legal_actions"] if a["action"]["type"] != "surrender"]
            rank = {"setup": 0, "bench": 1, "setup_ready": 2, "attack": 3, "end_turn": 4}
            actions.sort(key=lambda a: rank.get(a["type"], 9))
            action = actions[0]
        state = await command(api, view, uid, action)
    assert state["lobby"]["status"] == "finished"
    assert state["game"]["winner"] in ["1", "2"]
    assert "surrender" not in state["game"]["reason"].lower()
    ws1 = await api.client.ws_connect(f"/api/tcg/lobbies/{lid}/ws", headers=api.headers[1])
    assert (await ws1.receive_json())["game"]["winner"] == state["game"]["winner"]
    await ws1.close()
    state = await command(api, state, 1, {"type": "rematch"})
    assert state["game"] is None and not any(p["ready"] for p in state["lobby"]["players"])
