"""Loopback-only tabletop QA server with disposable accounts and a temporary DB.

Run from backend root: .venv/bin/python scripts/serve-tcg-test.py
No production/staging settings or player data are loaded. Open the two links
printed by the server to use separate localhost / 127.0.0.1 browser sessions.
"""
import asyncio
import json
from pathlib import Path
import random
import secrets
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from poke_pon_bot.db.base import Base
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.models.tcg_card import TcgCardDefinition
from poke_pon_bot.models.tcg_match import TcgLobby, TcgCommand, TcgSavedDeck
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.tcg_catalog import normalize_card, starter_deck
from poke_pon_bot.services.tcg_engine import new_game, apply_action
from poke_pon_bot.web.tcg_api import register_tcg_api
from poke_pon_bot.web.sessions import encode_session, set_session_cookie


async def app():
    temporary = tempfile.TemporaryDirectory(prefix="pokepon-tcg-qa-")
    engine = create_async_engine("sqlite+aiosqlite:///" + temporary.name + "/test.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tables = [RarityClass.__table__, Card.__table__, UserCardInstance.__table__, TcgCardDefinition.__table__,
              TcgLobby.__table__, TcgCommand.__table__, TcgSavedDeck.__table__]
    async with engine.begin() as db:
        await db.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
    source = Path("/tmp/pokepon-tcg-data/cards/en")
    if not source.is_dir():
        raise RuntimeError("Download PokemonTCG/pokemon-tcg-data to /tmp/pokepon-tcg-data before running this QA server.")
    raw = {c["id"]: c for f in source.glob("*.json") for c in json.loads(f.read_text())}
    normalized = {key: normalize_card(c) for key, c in raw.items()}
    starter = starter_deck(normalized)
    chosen = [raw[e["card_id"]] for e in starter]
    effects_entries = None
    if "--effects" in sys.argv:
        # Real cards, deterministic legal setup, disposable database only.
        ids = ["sm11-177", "bw6-114", "xy8-137", "xy1-121", "xy5-137", "sv1-167"]
        water = next(normalized[e["card_id"]] for e in starter if normalized[e["card_id"]]["supertype"] == "Pokémon")
        water_energy = next(normalized[e["card_id"]] for e in starter if normalized[e["card_id"]]["supertype"] == "Energy")
        effects_entries = [{"card_id": cid, "quantity": 4} for cid in [*ids, water["id"]]]
        effects_entries.append({"card_id": water_energy["id"], "quantity": 32})
        chosen = list({c["id"]: c for c in [*chosen, *(raw[cid] for cid in ids)]}.values())
    # Include an unsupported card so the visible coverage filter can be verified.
    chosen.append(next(c for key, c in raw.items() if not normalized[key]["supported"]))
    async with factory() as db:
        db.add(RarityClass(id=1, code="common", display_name="Common", sort_order=0))
        await db.flush()
        for i, card in enumerate(chosen, 1):
            db.add(Card(id=i, tcg_card_id=card["id"], name=card["name"], supertype=card["supertype"], tcg_subtypes=card.get("subtypes", []),
                        hp=card.get("hp"), tcg_types=card.get("types"), attacks=card.get("attacks"),
                        set_code=card["id"].rsplit("-", 1)[0], set_name="Browser QA catalog", collector_number=str(i),
                        image_small_url=card["images"]["small"], image_large_url=card["images"]["large"], rarity_class_id=1))
            await db.flush()
            db.add(TcgCardDefinition(tcg_card_id=card["id"], data=card, data_version="isolated-browser-test"))
            for uid in (1, 2):
                quantity = 60 if card["supertype"] == "Energy" else 4
                for _ in range(quantity):
                    db.add(UserCardInstance(discord_user_id=uid, card_id=i, public_id=secrets.token_hex(16), source="isolated-browser-test"))
        await db.commit()
        if effects_entries:
            deck = [normalized[e["card_id"]] for e in effects_entries for _ in range(e["quantity"])]
            for seed in range(10000):
                rng = random.Random(seed)
                game = new_game({"1": deck, "2": deck}, ["1", "2"], rng)
                game = apply_action(game, game["coin_winner"], {"type": "choose_start", "first_player": "1"}, rng)
                if not any(c["card"]["id"] == "sm11-177" for c in game["board"]["1"]["hand"]):
                    continue
                while game["phase"] == "setup":
                    choice = game["pending_choice"]
                    if choice:
                        game = apply_action(game, choice["player"], {"type": "choose", "choice_id": choice["id"], "selected": [choice["options"][0]["value"]]}, rng)
                        continue
                    for pid in ("1", "2"):
                        board = game["board"][pid]
                        if not board["active"]:
                            preferred = "sm11-177" if pid == "1" else water["id"]
                            basics = [c for c in board["hand"] if c["card"]["supertype"] == "Pokémon"]
                            active = next((c for c in basics if c["card"]["id"] == preferred), basics[0])
                            game = apply_action(game, pid, {"type": "setup", "active_uid": active["uid"]}, rng)
                        if not game["board"][pid]["setup_ready"]:
                            game = apply_action(game, pid, {"type": "setup_ready"}, rng)
                        if game["pending_choice"] or game["phase"] != "setup":
                            break
                if {"bw6-114", "xy5-137"} <= {c["card"]["id"] for c in game["board"]["1"]["hand"]}:
                    break
            else:
                raise RuntimeError("Could not create the deterministic effects scenario.")
            db.add(TcgLobby(id="effects-browser-qa", code="EFFECTS2", host_id=1, guest_id=2, status="active", version=0,
                data={"source": "global", "mixed": False, "players": [{"id": str(i), "name": f"Test Trainer {i}"} for i in (1, 2)],
                      "decks": {"1": effects_entries, "2": effects_entries}, "ready": ["1", "2"], "game": game}))
            await db.commit()
    secret = secrets.token_hex(32)
    settings = SimpleNamespace(pokepon_runtime="staging", web_session_secret=secret,
                               web_session_ttl_seconds=3600, web_allowed_origins=("http://localhost:8891", "http://127.0.0.1:8891"))
    @web.middleware
    async def loopback(request, handler):
        if request.remote not in ("127.0.0.1", "::1") or request.host not in ("localhost:8891", "127.0.0.1:8891"):
            raise web.HTTPForbidden()
        return await handler(request)
    application = web.Application(middlewares=[loopback])
    register_tcg_api(application, bot=SimpleNamespace(async_session_factory=factory), settings=settings)
    async def login(request):
        uid = int(request.match_info["id"])
        if uid not in (1, 2):
            raise web.HTTPNotFound()
        token = encode_session(secret, user_id=uid, username=f"Test Trainer {uid}", global_name=None, avatar_url=None)
        response = web.HTTPFound("/tcg/?api=http://" + request.host + ("&code=EFFECTS2" if effects_entries else ""))
        set_session_cookie(response, value=token, ttl_seconds=3600, secure=False)
        return response
    application.router.add_get("/__test__/login/{id}", login)
    site = Path.home() / "Documents/GitHub/hamiebrooklyn.github.io"
    application.router.add_get("/tcg/", lambda _: web.FileResponse(site / "tcg/index.html"))
    application.router.add_static("/assets/", site / "assets")
    async def cleanup(_):
        await engine.dispose()
        temporary.cleanup()
    application.on_cleanup.append(cleanup)
    print("Player 1: http://localhost:8891/__test__/login/1", flush=True)
    print("Player 2: http://127.0.0.1:8891/__test__/login/2", flush=True)
    return application


if __name__ == "__main__":
    web.run_app(app(), host="127.0.0.1", port=8891, access_log=None)
