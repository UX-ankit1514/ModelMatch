"""ModelMatch local proxy.

Run:  .venv/bin/python -m uvicorn proxy.app:create_app --factory --host 127.0.0.1 --port 8787

Endpoints
    GET  /health            liveness + configuration summary
    POST /session/start     register a Claude Code session (called by SessionStart)
    GET  /session/{id}      inspect a session's state (debugging)
    POST /recommend         ONE model recommendation for a prompt (called by UserPromptSubmit)
    POST /selection         record accept/reject for that recommendation
    POST /mock/api/route    local mock of the planned cloud contract
    *    /{anything else}   transparent passthrough to the Anthropic API (ANTHROPIC_BASE_URL target)

Fail-open rules for the passthrough: requests are forwarded untouched unless the
session has an accepted, routable recommendation for the current human turn. If
the upstream rejects a routed request, it is retried once with the original model.
"""

import hashlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import __version__
from .config import Settings, get_settings
from .provider import adapter_for, is_small_fast_model, resolve_model, same_model
from .router_client import MockRouter, RouterError, build_router
from .session_store import SessionStore

LOGGER_NAME = "modelmatch.proxy"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length",
}
# Upstream answers that mean "this model can't take this request" -> retry with the original.
RETRY_WITH_ORIGINAL = {400, 404, 422}
_SESSION_IN_USER_ID = re.compile(r"session_([0-9a-fA-F-]{36})")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def setup_logging(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(log_dir / "proxy.log"), maxBytes=1_000_000, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | proxy | %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


def fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def short(session_id: Optional[str]) -> str:
    return (session_id or "-")[:8]


def extract_session_id(headers, body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Find which Claude Code session an API request belongs to."""
    header_value = headers.get("x-claude-code-session-id")
    if header_value:
        return header_value, "header"
    metadata = body.get("metadata")
    user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
    if isinstance(user_id, str):
        if user_id.lstrip().startswith("{"):
            try:
                session_id = json.loads(user_id).get("session_id")
                if isinstance(session_id, str) and session_id:
                    return session_id, "metadata-json"
            except (ValueError, AttributeError):
                pass
        match = _SESSION_IN_USER_ID.search(user_id)
        if match:
            return match.group(1), "metadata"
    return None, None


def route_decision(original: object, target: str) -> Tuple[bool, str]:
    if not isinstance(original, str) or not original:
        return False, "request has no model"
    if is_small_fast_model(original):
        return False, "background helper request"
    if original == target or same_model(original, target):
        return False, "already on the recommended model"
    return True, ""


def request_shape(payload: Dict[str, Any]) -> str:
    """Describe a request's structure (field names, roles, block types) without any of its text."""
    parts = ["keys=" + ",".join(sorted(payload))]
    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        parts.append("thinking=" + str(thinking.get("type")))
    roles = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            kinds = "+".join(sorted({str(b.get("type")) for b in content if isinstance(b, dict)})) or "empty"
        else:
            kinds = "str"
        extra = [k for k in message if k not in ("role", "content")]
        roles.append("{}({}{})".format(message.get("role"), kinds, ";" + ",".join(extra) if extra else ""))
    if len(roles) > 12:
        roles = roles[:4] + ["...{} more...".format(len(roles) - 8)] + roles[-4:]
    parts.append("messages=[" + " ".join(roles) + "]")
    return " ".join(parts)


def anthropic_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"type": "error", "error": {"type": "api_error", "message": message}})


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SessionStartRequest(BaseModel):
    session_id: str
    cwd: Optional[str] = None
    source: Optional[str] = None
    model: Optional[str] = None


class RecommendRequest(BaseModel):
    session_id: str
    prompt: str
    prompt_id: Optional[str] = None
    cwd: Optional[str] = None


class SelectionRequest(BaseModel):
    session_id: str
    recommendation_id: str
    accepted: bool
    decided_by: str = "user"


class MockRouteRequest(BaseModel):
    prompt: str
    client: Optional[str] = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(settings: Optional[Settings] = None,
               upstream_transport: Optional[httpx.AsyncBaseTransport] = None,
               router_transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    settings = settings or get_settings()
    log = setup_logging(settings.log_dir)
    store = SessionStore(settings.state_file)
    router = build_router(settings, router_transport)
    mock_router = MockRouter()
    started_at = time.time()
    seen = {"logged_header_names": False}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.upstream = httpx.AsyncClient(
            transport=upstream_transport,
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0),
        )
        log.info("proxy started | pid=%s | router=%s | upstream=%s", os.getpid(), settings.router_mode,
                 settings.upstream_url)
        try:
            yield
        finally:
            await app.state.upstream.aclose()
            log.info("proxy stopped | pid=%s", os.getpid())

    app = FastAPI(title="ModelMatch proxy", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.settings = settings

    # -- ModelMatch endpoints ---------------------------------------------

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "modelmatch-proxy",
            "version": __version__,
            "router": settings.router_mode,
            "upstream": settings.upstream_url,
            "sessions": store.count(),
            "uptime_seconds": round(time.time() - started_at, 1),
            "pid": os.getpid(),
        }

    @app.post("/session/start")
    async def session_start(req: SessionStartRequest):
        store.start_session(req.session_id, req.cwd, req.source, req.model)
        log.info("session start | session=%s | source=%s | model=%s", short(req.session_id), req.source or "-",
                 req.model or "-")
        return {"ok": True, "session_id": req.session_id, "router": settings.router_mode}

    @app.get("/session/{session_id}")
    async def session_get(session_id: str):
        session = store.get(session_id)
        if session is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "unknown session"})
        return {"ok": True, "session": session}

    @app.post("/recommend")
    async def recommend(req: RecommendRequest):
        prompt = req.prompt
        if not prompt.strip():
            return JSONResponse(status_code=400, content={"ok": False, "error": "empty prompt"})
        fp = fingerprint(prompt)
        store.new_turn(req.session_id, req.prompt_id, fp)
        log.info("recommendation request | session=%s | prompt=%d chars fp=%s | router=%s",
                 short(req.session_id), len(prompt), fp, settings.router_mode)
        if settings.log_prompts:
            log.info("prompt preview | session=%s | %r", short(req.session_id), prompt[:80])

        try:
            result = await router.recommend(prompt)
        except RouterError as exc:
            log.warning("router failed | session=%s | %s | fallback: keep current model", short(req.session_id), exc)
            return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})
        except Exception as exc:  # never let a router bug escape as a 500 without logging
            log.exception("router crashed | session=%s | fallback: keep current model", short(req.session_id))
            return JSONResponse(status_code=502, content={"ok": False, "error": "router error: " + type(exc).__name__})

        model = resolve_model(result.recommended_model)
        routable, routing_note = adapter_for(model).can_route(model)
        current = store.current_model(req.session_id)
        recommendation = {
            "recommended_model": model.display,
            "recommended_raw": result.recommended_model,
            "model_id": model.model_id,
            "provider": model.provider,
            "recommendation_reason": result.reason,
            "confidence": result.confidence,
            "routable": routable,
            "routing_note": routing_note,
            "substituted_from": model.substituted_from,
            "router_source": result.source,
        }
        recommendation_id = store.set_recommendation(req.session_id, recommendation)
        same = routable and same_model(current, model.model_id)
        log.info("recommended | session=%s | %s (id=%s, provider=%s) | routable=%s | confidence=%s | source=%s%s",
                 short(req.session_id), model.display, model.model_id or "-", model.provider, routable,
                 result.confidence, result.source,
                 " | substituted from " + model.substituted_from if model.substituted_from else "")
        return {
            "ok": True,
            "recommendation_id": recommendation_id,
            **recommendation,
            "current_model": current,
            "current_model_display": resolve_model(current).display if current else None,
            "same_as_current": same,
        }

    @app.post("/selection")
    async def selection(req: SelectionRequest):
        result = store.set_selection(req.session_id, req.recommendation_id, req.accepted, req.decided_by)
        if not result.get("ok"):
            log.warning("selection ignored | session=%s | %s", short(req.session_id), result.get("error"))
            return JSONResponse(status_code=409, content=result)
        log.info("user %s | session=%s | decided_by=%s | route applied=%s%s",
                 "accepted" if req.accepted else "rejected", short(req.session_id), req.decided_by,
                 result["applied"], " -> " + result["active_model"] if result["applied"] else " (keep original model)")
        return result

    @app.post("/mock/api/route")
    async def mock_route(req: MockRouteRequest):
        result = await mock_router.recommend(req.prompt)
        return {"recommended_model": result.recommended_model, "reason": result.reason,
                "confidence": result.confidence}

    # -- Anthropic API passthrough (registered last) -----------------------------

    async def send_upstream(request: Request, url: str, headers: List[Tuple[str, str]], body: bytes):
        upstream_request = app.state.upstream.build_request(request.method, url, headers=headers, content=body)
        return await app.state.upstream.send(upstream_request, stream=True)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def passthrough(path: str, request: Request):
        body = await request.body()
        url = settings.upstream_url + "/" + path
        if request.url.query:
            url += "?" + request.url.query
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
        headers = [(k, v) for k, v in headers if k.lower() != "accept-encoding"] + [("accept-encoding", "gzip, deflate")]

        routed: Optional[Tuple[str, str, str]] = None  # (session_id, original, target)
        send_body = body
        is_messages = request.method == "POST" and path.rstrip("/") == "v1/messages"
        if is_messages and body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                session_id, how = extract_session_id(request.headers, payload)
                original = payload.get("model")
                if not seen["logged_header_names"]:
                    seen["logged_header_names"] = True
                    log.info("first API request seen | header names=%s | session id via %s",
                             sorted({k.lower() for k, _ in headers}), how or "NOT FOUND")
                if session_id:
                    if isinstance(original, str) and not is_small_fast_model(original):
                        store.note_seen_model(session_id, original)
                    target = store.active_route(session_id)
                    if target:
                        rewrite, why_not = route_decision(original, target)
                        if rewrite:
                            model = resolve_model(target)
                            send_body = json.dumps(adapter_for(model).apply(payload, model)).encode("utf-8")
                            routed = (session_id, str(original), target)
                            log.info("routing | session=%s | %s -> %s", short(session_id), original, target)
                        elif why_not != "background helper request":
                            log.info("not routed | session=%s | model=%s | %s", short(session_id), original, why_not)

        try:
            response = await send_upstream(request, url, headers, send_body)
            if routed and response.status_code in RETRY_WITH_ORIGINAL:
                detail = (await response.aread())[:300].decode("utf-8", "replace")
                await response.aclose()
                session_id, original, target = routed
                try:
                    log.info("rejected request shape | %s", request_shape(json.loads(send_body)))
                except ValueError:
                    pass
                log.warning("fallback triggered | session=%s | upstream rejected %s with HTTP %s (%s) | retrying with %s",
                            short(session_id), target, response.status_code, " ".join(detail.split()), original)
                store.mark_route_failed(session_id, "HTTP {} for {}".format(response.status_code, target))
                routed = None
                response = await send_upstream(request, url, headers, body)
        except httpx.HTTPError as exc:
            log.error("upstream error | %s %s | %s", request.method, "/" + path, type(exc).__name__)
            return anthropic_error(502, "ModelMatch proxy could not reach the upstream API ({}).".format(
                type(exc).__name__))

        if is_messages and response.status_code >= 400:
            log.warning("upstream HTTP %s | %s /%s", response.status_code, request.method, path)
        response_headers = {k: v for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP}
        if routed:
            response_headers["x-modelmatch-routed-model"] = routed[2]
        return StreamingResponse(response.aiter_raw(), status_code=response.status_code, headers=response_headers,
                                 background=BackgroundTask(response.aclose))

    return app
