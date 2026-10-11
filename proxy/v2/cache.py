"""Measuring what each request really used, and how warm each model's cache is.

UsageTap sits on the response stream the proxy already relays. It passes every
byte through untouched, and reads a copy for the usage numbers the API reports:

* Anthropic Messages (Claude Code): message_start / message_delta usage
  (input, cache read, cache write 5m/1h, output) and stop_reason (e.g. "refusal").
* OpenAI Responses (Codex): response.completed usage (input, cached input, output).

Only numbers and short status words are kept, never any text.

session_snapshot() turns those measurements into the current session's picture:
how much context the conversation carries, and whether the current model's and a
candidate model's caches are WARM / LIKELY_WARM / COLD / UNKNOWN.
"""

import json
import re
import time
import zlib
from typing import Any, Callable, Dict, Iterable, List, Optional

from .schema import CacheState, SessionSnapshot

MAX_JSON_BYTES = 4_000_000  # bigger non-streamed bodies are not inspected
DEFAULT_TTL = {"subscription": 3600, "api_key": 300, "unknown": 300}
TTL_SAFETY_SECONDS = 30
_TTL_RE = re.compile(rb'"ttl"\s*:\s*"(\d+)\s*([mh])"')
_RATE_PREFIX = "anthropic-ratelimit-unified-"
_SHORT_WORD = re.compile(r"^[a-z_]{1,24}$")


# ---------------------------------------------------------------------------
# Request metadata (no content)
# ---------------------------------------------------------------------------


def requested_ttl(body: bytes) -> Optional[int]:
    """The longest cache lifetime the request asks for (cache_control ttl), in seconds."""
    if b'"cache_control"' not in body:
        return None
    ttls = [int(n) * (3600 if unit == b"h" else 60) for n, unit in _TTL_RE.findall(body)]
    return max(ttls) if ttls else 300  # cache_control without a ttl = 5 minutes


def billing_mode(headers) -> str:
    """api_key | subscription | unknown, from which credential is present (never stored)."""
    if headers.get("x-api-key"):
        return "api_key"
    auth = headers.get("authorization") or ""
    beta = (headers.get("anthropic-beta") or "").lower()
    if auth.startswith("Bearer sk-ant-oat") or "oauth" in beta:
        return "subscription"
    if headers.get("chatgpt-account-id"):
        return "subscription"  # Codex with a ChatGPT sign-in
    if auth.startswith("Bearer sk-") and "ant" not in auth[:14]:
        return "api_key"  # OpenAI API key (Codex)
    return "unknown"


def rate_limits(headers) -> Dict[str, Any]:
    """Plan-limit numbers from the response headers: numeric values and short status words only."""
    found: Dict[str, Any] = {}
    for name, value in headers.items():
        name = name.lower()
        if not name.startswith(_RATE_PREFIX):
            continue
        key = name[len(_RATE_PREFIX):][:40]
        value = str(value).strip()
        try:
            found[key] = float(value)
        except ValueError:
            if _SHORT_WORD.match(value.lower()):
                found[key] = value.lower()
    return found


def usage_pressure(limits: Dict[str, Any]) -> Optional[float]:
    """Highest plan-limit utilization (0..1) among the reported values, if any."""
    values = []
    for key, value in limits.items():
        if "utilization" in key and isinstance(value, float):
            values.append(value / 100.0 if value > 1.0 else value)
    return round(max(values), 3) if values else None


# ---------------------------------------------------------------------------
# The tap
# ---------------------------------------------------------------------------


def _int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class UsageTap:
    """Wraps a byte stream; calls on_done(usage) once it ends. Never raises, never alters bytes."""

    def __init__(self, kind: str, content_encoding: str, content_type: str,
                 on_done: Callable[[Dict[str, Any]], None]):
        self.kind = kind  # "anthropic" | "responses"
        self.on_done = on_done
        self.sse = "event-stream" in (content_type or "")
        encoding = (content_encoding or "").lower()
        self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else \
            zlib.decompressobj() if encoding == "deflate" else None
        self._readable = encoding in ("", "identity", "gzip", "deflate")
        self._pending = b""
        self._body = bytearray()
        self.usage: Dict[str, Any] = {}
        self._done = False

    async def wrap(self, chunks):
        try:
            async for chunk in chunks:
                yield chunk
                self.feed(chunk)
        finally:
            self.finish()

    def feed(self, chunk: bytes) -> None:
        if not self._readable or self._done:
            return
        try:
            data = self._inflate.decompress(chunk) if self._inflate else chunk
            if self.sse:
                self._pending += data
                *lines, self._pending = self._pending.split(b"\n")
                for line in lines:
                    self._line(line)
            elif len(self._body) + len(data) <= MAX_JSON_BYTES:
                self._body.extend(data)
            else:
                self._readable = False
        except Exception:  # a parse problem must never touch the stream
            self._readable = False

    def _line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if b'"usage"' not in payload and b"stop_reason" not in payload and b"response.completed" not in payload:
            return
        try:
            event = json.loads(payload)
        except ValueError:
            return
        if isinstance(event, dict):
            self._event(event)

    def _event(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if self.kind == "anthropic":
            if kind == "message_start" and isinstance(event.get("message"), dict):
                self._anthropic_usage(event["message"].get("usage"))
            elif kind == "message_delta":
                self._anthropic_usage(event.get("usage"))
                delta = event.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                    self.usage["stop_reason"] = delta["stop_reason"][:24]
            elif "usage" in event:  # a non-streamed message
                self._anthropic_usage(event.get("usage"))
                if isinstance(event.get("stop_reason"), str):
                    self.usage["stop_reason"] = event["stop_reason"][:24]
        else:
            response = event.get("response") if kind and str(kind).startswith("response.") else event
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                usage = response["usage"]
                details = usage.get("input_tokens_details") or {}
                total_in = _int(usage.get("input_tokens"))
                cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else None
                self.usage.update(input_tokens=(total_in - (cached or 0)) if total_in is not None else None,
                                  cache_read_tokens=cached, cache_write_tokens=None,
                                  output_tokens=_int(usage.get("output_tokens")))
                if isinstance(response.get("status"), str):
                    self.usage["stop_reason"] = response["status"][:24]

    def _anthropic_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for source, target in (("input_tokens", "input_tokens"), ("cache_read_input_tokens", "cache_read_tokens"),
                               ("cache_creation_input_tokens", "cache_write_tokens"),
                               ("output_tokens", "output_tokens")):
            value = _int(usage.get(source))
            if value is not None:
                self.usage[target] = max(value, self.usage.get(target) or 0)
        split = usage.get("cache_creation")
        if isinstance(split, dict):
            for source, target in (("ephemeral_5m_input_tokens", "cache_write_5m"),
                                   ("ephemeral_1h_input_tokens", "cache_write_1h")):
                value = _int(split.get(source))
                if value is not None:
                    self.usage[target] = value

    def finish(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            if self._readable and not self.sse and self._body:
                try:
                    data = json.loads(bytes(self._body))
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    self._event(data)
            if self._readable and self.sse and self._pending:
                self._line(self._pending)
            self.on_done(dict(self.usage))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session snapshot
# ---------------------------------------------------------------------------


def context_tokens(event: Dict[str, Any]) -> Optional[int]:
    parts = [event.get(k) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")]
    known = [p for p in parts if isinstance(p, int)]
    return sum(known) if known else None


def _main_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Requests that carried the main conversation (not background helpers or sub-agents)."""
    labelled = [e for e in events if e.get("request_class")]
    if labelled:
        main = [e for e in labelled if str(e["request_class"]).lower() in ("main", "main_thread", "primary", "agent")]
        return main or events
    return events


def cache_state(events: List[Dict[str, Any]], model: Optional[str], same: Callable[[str, str], bool],
                at: float, measured_session: bool) -> str:
    if not model:
        return CacheState.UNKNOWN
    mine = [e for e in events if e.get("sent_model") and same(e["sent_model"], model) and (e.get("status") or 0) < 400]
    if not mine:
        return CacheState.COLD if measured_session else CacheState.UNKNOWN
    last = mine[-1]
    ttl = last.get("requested_ttl") or DEFAULT_TTL.get(last.get("billing_mode") or "unknown", 300)
    if at - (last.get("ts") or 0) > ttl - TTL_SAFETY_SECONDS:
        return CacheState.COLD
    if (last.get("cache_read_tokens") or 0) > 0 or (last.get("cache_write_tokens") or 0) > 0:
        return CacheState.WARM
    return CacheState.LIKELY_WARM


def session_snapshot(events: Iterable[Dict[str, Any]], client: str, session_id: str,
                     current_model: Optional[str], effective_model: Optional[str], candidate_model: Optional[str],
                     same: Callable[[str, str], bool], measures_cache: bool,
                     at: Optional[float] = None) -> SessionSnapshot:
    at = at or time.time()
    events = sorted((e for e in events if (e.get("status") or 0) < 400 and e.get("request_class") != "background"),
                    key=lambda e: e.get("ts") or 0)
    snapshot = SessionSnapshot(session_id=session_id, client=client, current_model=current_model,
                               effective_model=effective_model)
    if not measures_cache:
        return snapshot  # e.g. Copilot: its traffic never passes through ModelMatch
    snapshot.observable = True
    snapshot.measured = bool(events)
    main = _main_events(events)
    running = [e for e in main if effective_model and e.get("sent_model") and same(e["sent_model"], effective_model)]
    latest = (running or main)[-1:] if main else []
    if latest:
        # The biggest request of the latest turn is the conversation itself (helpers carry less).
        turn = [e for e in main if e.get("decision_id") and e.get("decision_id") == latest[0].get("decision_id")]
        sizes = [context_tokens(e) for e in (turn or latest)]
        sizes = [s for s in sizes if s is not None]
        snapshot.estimated_context_tokens = max(sizes) if sizes else None
        snapshot.cached_input_tokens = latest[0].get("cache_read_tokens")
        snapshot.cache_creation_tokens = latest[0].get("cache_write_tokens")
        snapshot.billing_mode = latest[0].get("billing_mode") or "unknown"
    snapshot.cache_state_current = cache_state(events, effective_model, same, at, snapshot.measured)
    snapshot.cache_state_candidate = cache_state(events, candidate_model, same, at, snapshot.measured)
    pressures = []
    for event in events[-20:]:
        limits = event.get("rate_limits")
        if isinstance(limits, str):
            try:
                limits = json.loads(limits)
            except ValueError:
                limits = None
        if isinstance(limits, dict):
            value = usage_pressure(limits)
            if value is not None:
                pressures.append(value)
    snapshot.usage_pressure = pressures[-1] if pressures else None
    return snapshot
