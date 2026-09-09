"""Authenticated, staging-gated TCG HTTP and private WebSocket endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import WSMsgType, web

from poke_pon_bot.services.tcg_matches import TcgError, TcgService
from poke_pon_bot.web.sessions import decode_session, read_session

_LOG = logging.getLogger(__name__)
TCG_SERVICE_KEY = web.AppKey("tcg_service", TcgService)


def register_tcg_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    enabled = getattr(settings, "pokepon_runtime", "production") == "staging" or os.environ.get("WEB_TCG_ENABLED") == "1"
    if not enabled or not settings.web_session_secret:
        return
    service = TcgService(bot.async_session_factory)
    # Kept on the app for integration tests and explicit cache invalidation.
    app[TCG_SERVICE_KEY] = service
    rooms: dict[str, dict[web.WebSocketResponse, int]] = {}
    secret, ttl = settings.web_session_secret, settings.web_session_ttl_seconds
    origins = {o.rstrip("/") for o in settings.web_allowed_origins}

    def user(request):
        session = read_session(request, secret, max_age=ttl)
        if session is None:
            raise TcgError("Sign in with Discord to play.", 401)
        return session

    def check_origin(request):
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin not in origins:
            raise TcgError("This website origin is not allowed.", 403)
        # Cookie-authenticated browser commands always include an Origin. A
        # signed Bearer token is allowed for non-browser API clients and tests.
        if request.method != "GET" and not origin and not request.headers.get("Authorization"):
            raise TcgError("A trusted Origin or Bearer session is required.", 403)

    async def body(request):
        if request.content_length and request.content_length > 32768:
            raise TcgError("Request body is too large.", 413)
        chunks, size = [], 0
        async for chunk in request.content.iter_chunked(8192):
            size += len(chunk)
            if size > 32768:
                raise TcgError("Request body is too large.", 413)
            chunks.append(chunk)
        try:
            value = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise TcgError("Request body must be valid JSON.")
        if not isinstance(value, dict):
            raise TcgError("Request body must be a JSON object.")
        return value

    def safe(handler):
        async def wrapper(request):
            try:
                check_origin(request)
                response = await handler(request)
                if not response.prepared:
                    response.headers["Cache-Control"] = "no-store"
                return response
            except TcgError as exc:
                return web.json_response({"error": str(exc), "details": exc.details}, status=exc.status, headers={"Cache-Control": "no-store"})
            except Exception:
                _LOG.exception("TCG request failed: %s %s", request.method, request.path)
                return web.json_response({"error": "The TCG service could not complete this request. Please retry."}, status=500,
                    headers={"Cache-Control": "no-store"})
        return wrapper

    async def broadcast(lobby_id):
        for ws, uid in list(rooms.get(lobby_id, {}).items()):
            try:
                # Separate projection for each viewer, including forced choices.
                await ws.send_json({"type": "state", **await service.get(lobby_id, uid)})
            except Exception:
                rooms.get(lobby_id, {}).pop(ws, None)
                await ws.close()

    async def me(request):
        sess = user(request)
        return web.json_response({"user": {"id": str(sess.user_id), "name": sess.global_name or sess.username}})

    async def lobbies(request):
        sess = user(request)
        if request.method == "GET":
            return web.json_response({"lobbies": await service.list(sess.user_id)})
        data = await body(request)
        return web.json_response(await service.create(sess.user_id, sess.global_name or sess.username,
            data.get("source", "global"), data.get("mixed", False)), status=201)

    async def join(request):
        sess, data = user(request), await body(request)
        result = await service.join(sess.user_id, sess.global_name or sess.username, data.get("code", ""))
        await broadcast(result["lobby"]["id"])
        return web.json_response(result)

    async def detail(request):
        sess, lid = user(request), request.match_info["id"]
        await service.touch(lid, sess.user_id)
        return web.json_response(await service.get(lid, sess.user_id))

    async def command(request):
        sess, lid, data = user(request), request.match_info["id"], await body(request)
        result = await service.command(lid, sess.user_id, data)
        await broadcast(lid)
        return web.json_response(result)

    async def catalog(request):
        sess, q = user(request), request.query
        try:
            page = int(q.get("page", "1"))
        except ValueError:
            raise TcgError("Page must be a whole number.")
        return web.json_response(await service.catalog(sess.user_id, q.get("lobby_id"), q.get("q", ""),
            q.get("supertype", ""), q.get("supported") == "1", page, set(q["ids"].split(",")[:60]) if q.get("ids") else None))

    async def starter(request):
        return web.json_response(await service.starter(user(request).user_id, request.query.get("lobby_id")))

    async def decks(request):
        uid = user(request).user_id
        if request.method == "GET":
            return web.json_response({"decks": await service.decks(uid)})
        return web.json_response(await service.save_deck(uid, await body(request)))

    async def websocket(request):
        lid = request.match_info["id"]
        sess = read_session(request, secret, max_age=ttl)
        auth_token = None
        if sess is not None:
            await service.get(lid, sess.user_id)
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=32768)
        await ws.prepare(request)
        if sess is None:
            try:
                first = await ws.receive(timeout=10)
                payload = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
                if payload.get("type") == "auth" and isinstance(payload.get("token"), str):
                    auth_token = payload["token"]
                    sess = decode_session(secret, auth_token, max_age=ttl)
                if sess is None:
                    await ws.close(code=4001, message=b"Sign in required")
                    return ws
                await service.get(lid, sess.user_id)
            except Exception:
                await ws.close(code=4003, message=b"Lobby access denied")
                return ws
        uid = sess.user_id
        rooms.setdefault(lid, {})[ws] = uid
        await service.touch(lid, uid)
        await ws.send_json({"type": "state", **await service.get(lid, uid)})

        async def presence():
            # Heartbeats also persist presence after a server restart. WebSocket
            # command payloads never carry an actor ID accepted by the server.
            while not ws.closed:
                await asyncio.sleep(25)
                valid = decode_session(secret, auth_token, max_age=ttl) if auth_token else read_session(request, secret, max_age=ttl)
                if valid is None:
                    await ws.close(code=4001, message=b"Session expired; sign in again")
                    break
                await service.touch(lid, uid)
                await ws.send_json({"type": "state", **await service.get(lid, uid)})

        task = asyncio.create_task(presence())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                        if not isinstance(payload, dict):
                            raise TcgError("Expected an object.")
                        if payload.get("type") in ("ping", "sync"):
                            await service.touch(lid, uid)
                            await ws.send_json({"type": "state", **await service.get(lid, uid)})
                        elif payload.get("type") == "auth":
                            # Existing cookie session wins; never switch actor mid-connection.
                            continue
                        else:
                            await service.command(lid, uid, payload)
                            await broadcast(lid)
                    except (TcgError, ValueError) as exc:
                        await ws.send_json({"type": "error", "error": str(exc), "details": getattr(exc, "details", [])})
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            rooms.get(lid, {}).pop(ws, None)
            if not rooms.get(lid):
                rooms.pop(lid, None)
        return ws

    async def cleanup(_app):
        await asyncio.gather(*(ws.close(code=1001, message=b"Server restarting; reconnect shortly")
            for room in list(rooms.values()) for ws in list(room)), return_exceptions=True)

    app.on_shutdown.append(cleanup)
    for method, path, handler in [
        ("GET", "/api/tcg/me", me), ("GET", "/api/tcg/catalog", catalog),
        ("GET", "/api/tcg/starter-deck", starter), ("GET", "/api/tcg/decks", decks), ("POST", "/api/tcg/decks", decks),
        ("GET", "/api/tcg/lobbies", lobbies), ("POST", "/api/tcg/lobbies", lobbies),
        ("POST", "/api/tcg/lobbies/join", join), ("GET", "/api/tcg/lobbies/{id}", detail),
        ("POST", "/api/tcg/lobbies/{id}/commands", command), ("GET", "/api/tcg/lobbies/{id}/ws", websocket),
    ]:
        app.router.add_route(method, path, safe(handler))
