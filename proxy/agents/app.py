"""ModelMatch proxy for Codex CLI and GitHub Copilot CLI (the "agents proxy").

Run:  .venv/bin/python -m uvicorn proxy.agents.app:create_app --factory --host 127.0.0.1 --port 8788 --ws none

Endpoints
    GET  /health                 liveness + configuration summary
    GET  /models/{client}        the models ModelMatch can switch to, and its light/standard/heavy picks
    POST /session/start          register a Codex / Copilot session (SessionStart hooks)
    GET  /session/{client}/{id}  inspect a session's state (debugging)
    POST /recommend              ONE recommendation for a prompt, matched to the tool's own models
    POST /selection              record accept/reject for that recommendation
    *    /v1/{anything}          Codex only: passthrough to Codex's model API (openai_base_url target)

Copilot switches models itself (its extension calls Copilot's own model switch), so
only Codex's traffic passes through here. Codex requests are forwarded untouched
unless the session accepted a recommendation for the *current turn*; then that
turn's requests use the recommended model. If the upstream rejects a routed request,
it is retried once, unchanged. WebSocket upgrades are refused with 426 so Codex uses
HTTPS straight away (the model can only be swapped in requests the proxy can read).
"""

import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .. import __version__
from ..app import HOP_BY_HOP, RETRY_WITH_ORIGINAL
from ..router_client import RouterError
from ..session_store import SessionStore
from .catalog import ModelInfo, codex_models, detect_tiers, find_model, mock_tier, resolve, with_pinned
from .codex_config import model_list_file, read_config
from .config import CLIENTS, AgentSettings, get_agent_settings
from .responses import adapt_request, decode_body, request_identity, zstd_available
from .router import build_agent_router

LOGGER_NAME = "modelmatch.agents"
SERVICE_NAME = "modelmatch-agents-proxy"
CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"
OPENAI_UPSTREAM = "https://api.openai.com/v1"
WHERE = {"codex": "your Codex model list", "copilot": "your Copilot plan"}


def setup_logging(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(log_dir / "agents-proxy.log"), maxBytes=1_000_000, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | agents | %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


def fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def short(session_id: Optional[str]) -> str:
    return (session_id or "-")[:8]


def store_key(client: str, session_id: str) -> str:
    return client + ":" + session_id


def openai_error(status: int, message: str, kind: str = "api_error") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": kind}})


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SessionStartRequest(BaseModel):
    client: str
    session_id: str
    cwd: Optional[str] = None
    source: Optional[str] = None
    model: Optional[str] = None


class AvailableModel(BaseModel):
    id: str
    name: Optional[str] = None
    category: Optional[str] = None


class RecommendRequest(BaseModel):
    client: str
    session_id: str
    prompt: str
    turn_id: Optional[str] = None
    cwd: Optional[str] = None
    current_model: Optional[str] = None
    available_models: Optional[List[AvailableModel]] = None


class SelectionRequest(BaseModel):
    client: str
    session_id: str
    recommendation_id: str
    accepted: bool
    decided_by: str = "user"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(settings: Optional[AgentSettings] = None,
               upstream_transport: Optional[httpx.AsyncBaseTransport] = None,
               router_transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    settings = settings or get_agent_settings()
    base = settings.base
    log = setup_logging(base.log_dir)
    store = SessionStore(base.state_file)
    routers = {client: build_agent_router(base, client, router_transport) for client in CLIENTS}
    started_at = time.time()
    seen = {"logged_header_names": False, "warned_ws": False, "warned_compressed": False}
    codex_cache: Dict[str, object] = {"key": None, "models": [], "path": None}

    def codex_catalog() -> Tuple[List[ModelInfo], object]:
        """Codex's model list, re-read whenever Codex updates the file."""
        path = model_list_file(settings.codex_home, read_config(settings.codex_home))
        try:
            stamp = (str(path), path.stat().st_mtime)
        except OSError:
            stamp = (str(path), None)
        if codex_cache["key"] != stamp:
            models, path = codex_models(settings.codex_home)
            codex_cache.update(key=stamp, models=models, path=path)
        return codex_cache["models"], codex_cache["path"]  # type: ignore[return-value]

    def codex_default_model() -> Optional[str]:
        model = read_config(settings.codex_home).get("model")
        return model if isinstance(model, str) and model.strip() else None

    def models_for(client: str, req: Optional[RecommendRequest] = None) -> List[ModelInfo]:
        if client == "codex":
            models = list(codex_catalog()[0])
            current = (req.current_model if req else None) or codex_default_model()
            if current and not find_model(models, current):
                models.append(ModelInfo(id=current))
            return models
        offered = (req.available_models if req else None) or []
        return [ModelInfo(id=m.id, name=m.name or "", category=m.category or "") for m in offered if m.id]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.upstream = httpx.AsyncClient(
            transport=upstream_transport,
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0),
        )
        log.info("proxy started | pid=%s | router=%s | codex upstream=%s | zstd=%s", os.getpid(), base.router_mode,
                 settings.codex_upstream_url or "auto", "yes" if zstd_available() else "no")
        try:
            yield
        finally:
            await app.state.upstream.aclose()
            log.info("proxy stopped | pid=%s", os.getpid())

    app = FastAPI(title="ModelMatch agents proxy", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.settings = settings

    def bad_client(client: str) -> Optional[JSONResponse]:
        if client in CLIENTS:
            return None
        return JSONResponse(status_code=400, content={"ok": False, "error": "unknown client " + repr(client)})

    # -- ModelMatch endpoints ---------------------------------------------

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "version": __version__,
            "router": base.router_mode,
            "codex_upstream": settings.codex_upstream_url or "auto",
            "zstd": zstd_available(),
            "sessions": store.count(),
            "uptime_seconds": round(time.time() - started_at, 1),
            "pid": os.getpid(),
        }

    @app.get("/models/{client}")
    async def models(client: str):
        problem = bad_client(client)
        if problem:
            return problem
        listed = models_for(client)
        current = codex_default_model() if client == "codex" else None
        tiers = detect_tiers(listed, current, settings.tier_overrides.get(client))
        source = str(codex_catalog()[1]) if client == "codex" else "sent by the Copilot extension per prompt"
        return {"ok": True, "client": client, "models": [m.id for m in listed], "tiers": tiers, "source": source}

    @app.post("/session/start")
    async def session_start(req: SessionStartRequest):
        problem = bad_client(req.client)
        if problem:
            return problem
        key = store_key(req.client, req.session_id)
        existing = store.get(key)
        if existing and existing.get("turn") and req.source == "startup":
            # Codex can run SessionStart alongside the first prompt's hook: don't undo that prompt's choice.
            if req.model:
                store.note_seen_model(key, req.model)
        else:
            store.start_session(key, req.cwd, req.source, req.model)
        log.info("session start | %s | session=%s | source=%s | model=%s", req.client, short(req.session_id),
                 req.source or "-", req.model or "-")
        return {"ok": True, "session_id": req.session_id, "router": base.router_mode}

    @app.get("/session/{client}/{session_id}")
    async def session_get(client: str, session_id: str):
        session = store.get(store_key(client, session_id))
        if session is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "unknown session"})
        return {"ok": True, "session": session}

    @app.post("/recommend")
    async def recommend(req: RecommendRequest):
        problem = bad_client(req.client)
        if problem:
            return problem
        prompt = req.prompt
        if not prompt.strip():
            return JSONResponse(status_code=400, content={"ok": False, "error": "empty prompt"})
        key = store_key(req.client, req.session_id)
        fp = fingerprint(prompt)
        store.new_turn(key, req.turn_id, fp)
        if req.current_model:
            store.note_seen_model(key, req.current_model)
        log.info("recommendation request | %s | session=%s | prompt=%d chars fp=%s | router=%s", req.client,
                 short(req.session_id), len(prompt), fp, base.router_mode)
        if base.log_prompts:
            log.info("prompt preview | session=%s | %r", short(req.session_id), prompt[:80])

        try:
            result = await routers[req.client].recommend(prompt)
        except RouterError as exc:
            log.warning("router failed | session=%s | %s | fallback: keep current model", short(req.session_id), exc)
            return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})
        except Exception as exc:  # never let a router bug escape as a 500 without logging
            log.exception("router crashed | session=%s | fallback: keep current model", short(req.session_id))
            return JSONResponse(status_code=502, content={"ok": False, "error": "router error: " + type(exc).__name__})

        current = req.current_model or store.current_model(key)
        listed = models_for(req.client, req)
        tiers = detect_tiers(listed, current, settings.tier_overrides.get(req.client))
        listed = with_pinned(listed, tiers)
        size = mock_tier(result.recommended_model) if result.source == "mock" else None
        match = resolve(result.recommended_model, listed, tiers, size, WHERE[req.client])
        recommendation = {
            "recommended_model": match.display,
            "recommended_raw": result.recommended_model,
            "model_id": match.model_id,
            "provider": req.client,
            "recommendation_reason": result.reason,
            "confidence": result.confidence,
            "routable": match.routable,
            "routing_note": match.note,
            "substituted_from": match.substituted_from,
            "router_source": result.source,
        }
        recommendation_id = store.set_recommendation(key, recommendation)
        same = bool(match.routable and current and match.model_id and match.model_id.lower() == current.lower())
        log.info("recommended | %s | session=%s | %s (id=%s) | routable=%s | same_as_current=%s | source=%s%s",
                 req.client, short(req.session_id), match.display, match.model_id or "-", match.routable, same,
                 result.source, " | named " + match.substituted_from if match.substituted_from else "")
        return {"ok": True, "recommendation_id": recommendation_id, **recommendation, "current_model": current,
                "same_as_current": same}

    @app.post("/selection")
    async def selection(req: SelectionRequest):
        problem = bad_client(req.client)
        if problem:
            return problem
        result = store.set_selection(store_key(req.client, req.session_id), req.recommendation_id, req.accepted,
                                     req.decided_by)
        if not result.get("ok"):
            log.warning("selection ignored | %s | session=%s | %s", req.client, short(req.session_id),
                        result.get("error"))
            return JSONResponse(status_code=409, content=result)
        log.info("user %s | %s | session=%s | decided_by=%s | route applied=%s%s",
                 "accepted" if req.accepted else "rejected", req.client, short(req.session_id), req.decided_by,
                 result["applied"], " -> " + result["active_model"] if result["applied"] else " (keep original model)")
        return result

    # -- Codex model API passthrough ---------------------------------------------

    def codex_upstream(headers) -> str:
        if settings.codex_upstream_url:
            return settings.codex_upstream_url
        # Codex sends this header only with a ChatGPT sign-in; those go to the ChatGPT backend.
        return CHATGPT_UPSTREAM if headers.get("chatgpt-account-id") else OPENAI_UPSTREAM

    async def send_upstream(request: Request, url: str, headers: List[Tuple[str, str]], body: bytes):
        upstream_request = app.state.upstream.build_request(request.method, url, headers=headers, content=body)
        return await app.state.upstream.send(upstream_request, stream=True)

    def route_for(session_id: str, turn_id: Optional[str], original: object) -> Optional[str]:
        key = store_key("codex", session_id)
        target = store.active_route(key)
        if not target:
            return None
        accepted_turn = ((store.get(key) or {}).get("turn") or {}).get("prompt_id")
        if accepted_turn and turn_id and accepted_turn != turn_id:
            return None  # a later turn: the choice was for one prompt only
        if not isinstance(original, str) or original.lower() == target.lower():
            return None
        return target

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def codex_passthrough(path: str, request: Request):
        if request.headers.get("upgrade", "").lower() == "websocket":
            if not seen["warned_ws"]:
                seen["warned_ws"] = True
                log.info("websocket upgrade refused (426) | Codex falls back to HTTPS")
            return openai_error(426, "ModelMatch proxy: use HTTPS for this endpoint.", "upgrade_required")

        body = await request.body()
        url = codex_upstream(request.headers) + "/" + path
        if request.url.query:
            url += "?" + request.url.query
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
        headers = [(k, v) for k, v in headers if k.lower() != "accept-encoding"] + [("accept-encoding", "gzip, deflate")]

        routed: Optional[Tuple[str, str, str]] = None  # (session_id, original, target)
        send_body, send_headers = body, headers
        if request.method == "POST" and path.rstrip("/") == "responses" and body:
            encoding = request.headers.get("content-encoding", "")
            plain = decode_body(body, encoding)
            payload = None
            if plain is not None:
                try:
                    payload = json.loads(plain)
                except ValueError:
                    payload = None
            elif not seen["warned_compressed"]:
                seen["warned_compressed"] = True
                log.warning("cannot read %s-encoded Codex requests%s | they pass through without model switching",
                            encoding, "" if zstd_available() else " (zstandard not installed: run install_codex.sh)")
            if isinstance(payload, dict):
                session_id, turn_id, kind = request_identity(request.headers, payload)
                original = payload.get("model")
                if not seen["logged_header_names"]:
                    seen["logged_header_names"] = True
                    log.info("first Codex request seen | header names=%s | session id %s | encoding=%s",
                             sorted({k.lower() for k, _ in headers}), "found" if session_id else "NOT FOUND",
                             encoding or "none")
                if session_id and kind in (None, "turn"):
                    if isinstance(original, str):
                        store.note_seen_model(store_key("codex", session_id), original)
                    target = route_for(session_id, turn_id, original)
                    if target:
                        info = find_model(codex_catalog()[0], target) or ModelInfo(id=target)
                        send_body = json.dumps(adapt_request(payload, info)).encode("utf-8")
                        send_headers = [(k, v) for k, v in headers if k.lower() != "content-encoding"]
                        routed = (session_id, str(original), target)
                        log.info("routing | codex | session=%s | turn=%s | %s -> %s", short(session_id),
                                 short(turn_id), original, target)

        try:
            response = await send_upstream(request, url, send_headers, send_body)
            if routed and response.status_code in RETRY_WITH_ORIGINAL:
                detail = (await response.aread())[:300].decode("utf-8", "replace")
                await response.aclose()
                session_id, original, target = routed
                log.warning("fallback triggered | session=%s | upstream rejected %s with HTTP %s (%s) | retrying with %s",
                            short(session_id), target, response.status_code, " ".join(detail.split()), original)
                store.mark_route_failed(store_key("codex", session_id),
                                        "HTTP {} for {}".format(response.status_code, target))
                routed = None
                response = await send_upstream(request, url, headers, body)
        except httpx.HTTPError as exc:
            log.error("upstream error | %s /v1/%s | %s", request.method, path, type(exc).__name__)
            return openai_error(502, "ModelMatch proxy could not reach the upstream API ({}).".format(
                type(exc).__name__))

        if response.status_code >= 400 and path.rstrip("/") == "responses":
            log.warning("upstream HTTP %s | %s /v1/%s", response.status_code, request.method, path)
        response_headers = {k: v for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP}
        if routed:
            response_headers["x-modelmatch-routed-model"] = routed[2]
        return StreamingResponse(response.aiter_raw(), status_code=response.status_code, headers=response_headers,
                                 background=BackgroundTask(response.aclose))

    return app
