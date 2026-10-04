"""Clients that ask ModelMatch for ONE best-fit model.

* MockRouter          - local heuristics, used when MODELMATCH_API_URL is empty.
* CloudRouteClient    - the planned production contract:
                          POST /api/route  {"prompt": "...", "client": "claude-code"}
                          -> {"recommended_model": "...", "reason": "...", "confidence": 0.0}
* CloudAnalyzeClient  - adapter for the endpoint the live site exposes today:
                          POST /api/analyze {"prompt": "..."} -> chat completion whose text
                          contains "SECTION 1: RECOMMENDED MODEL <model> <tier> - <reason>".
"""

import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import Settings
from .provider import strip_tier

CLIENT_NAME = "claude-code"
MAX_REASON_CHARS = 300


@dataclass(frozen=True)
class RouterResult:
    recommended_model: str
    reason: str
    confidence: Optional[float]
    source: str


class RouterError(Exception):
    """The router could not produce a usable recommendation."""


def _clean_reason(reason: object) -> str:
    if not isinstance(reason, str):
        return ""
    reason = " ".join(reason.split())
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[: MAX_REASON_CHARS - 1].rstrip() + "…"
    return reason


def _clean_confidence(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if 0.0 <= value <= 1.0 else None


def _validate_model_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RouterError("response has no recommended_model")
    name = " ".join(name.split())
    if len(name) > 120:
        raise RouterError("recommended_model is too long to be a model name")
    return name


# ---------------------------------------------------------------------------
# Mock router
# ---------------------------------------------------------------------------

_TEST_OVERRIDE = re.compile(r"wc-test:\s*([A-Za-z0-9][A-Za-z0-9 ._-]{0,60}?)\s*(?:$|[\n;,])", re.IGNORECASE)
_COMPLEX_HINTS = (
    "architecture", "architect", "system design", "design a system", "from scratch", "end-to-end",
    "end to end", "migrate", "migration", "refactor the entire", "whole codebase", "entire codebase",
    "trade-off", "tradeoff", "step-by-step plan", "security audit", "threat model", "prove",
    "research", "strategy", "roadmap", "multi-step", "in depth", "in-depth", "comprehensive",
)
_CODE_HINTS = (
    "code", "function", "bug", "error", "fix", "debug", "test", "python", "javascript", "typescript",
    "react", "api", "refactor", "implement", "class", "script", "sql", "component", "deploy",
    "stack trace", "exception", "compile", "build", "endpoint", "regex", "css", "html", "git",
)


def _has_hint(text: str, hints) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(h) + r"(?![a-z])", text) for h in hints)


class MockRouter:
    """Deterministic stand-in for the cloud router, good enough to exercise the full flow."""

    source = "mock"

    async def recommend(self, prompt: str) -> RouterResult:
        override = _TEST_OVERRIDE.search(prompt)
        if override:
            return RouterResult(override.group(1).strip(), "Test override requested in the prompt.", 1.0, self.source)

        text = prompt.lower()
        words = len(prompt.split())
        if words > 400 or (words >= 8 and _has_hint(text, _COMPLEX_HINTS)):
            return RouterResult("Claude Opus 5.5", "Complex, multi-step reasoning benefits from the most capable model.",
                                0.72, self.source)
        if words > 60 or _has_hint(text, _CODE_HINTS):
            return RouterResult("Claude Sonnet 5.5", "Coding or multi-step task: a strong coding model with good speed.",
                                0.7, self.source)
        return RouterResult("Claude Haiku 4.5", "Short, simple request: a fast, lightweight model is enough.",
                            0.8, self.source)


# ---------------------------------------------------------------------------
# Cloud routers
# ---------------------------------------------------------------------------


class _CloudClient:
    source = "cloud"

    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.url = settings.api_url
        self.timeout = settings.api_timeout
        self.max_prompt_chars = settings.max_prompt_chars
        self.api_key = settings.api_key
        self.transport = transport

    def _payload(self, prompt: str) -> dict:
        raise NotImplementedError

    def _parse(self, data: object) -> RouterResult:
        raise NotImplementedError

    async def recommend(self, prompt: str) -> RouterResult:
        headers = {"Content-Type": "application/json", "User-Agent": "modelmatch-claude-code/0.1"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        # Send only what routing needs: the (length-capped) prompt.
        payload = self._payload(prompt[: self.max_prompt_chars])
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(self.url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise RouterError("router API timed out after {:g}s".format(self.timeout))
        except httpx.HTTPError as exc:
            raise RouterError("could not reach router API ({})".format(type(exc).__name__))

        if response.status_code != 200:
            raise RouterError("router API returned HTTP {}".format(response.status_code))
        try:
            data = response.json()
        except ValueError:
            raise RouterError("router API returned a non-JSON response")
        return self._parse(data)


class CloudRouteClient(_CloudClient):
    source = "cloud-route"

    def _payload(self, prompt: str) -> dict:
        return {"prompt": prompt, "client": CLIENT_NAME}

    def _parse(self, data: object) -> RouterResult:
        if not isinstance(data, dict):
            raise RouterError("response is not a JSON object")
        model = _validate_model_name(data.get("recommended_model"))
        return RouterResult(model, _clean_reason(data.get("reason")), _clean_confidence(data.get("confidence")),
                            self.source)


_SECTION1_RE = re.compile(r"^(?:SECTION\s*1\s*[:.\-]?\s*)?RECOMMENDED\s*MODEL\s*[:.\-]?\s*(.*)$", re.IGNORECASE)
_NEXT_SECTION_RE = re.compile(r"^(SECTION\s*\d|TOKEN\s*RISK|WORKFLOW\s*ADVICE|OPTIMIZED\s*PROMPT)", re.IGNORECASE)


def parse_analyze_text(text: str):
    """Extract (model, reason) from the analyzer's 'RECOMMENDED MODEL' section."""
    capturing = False
    for line in text.splitlines():
        clean = line.strip().strip("*#_` ").strip()
        match = _SECTION1_RE.match(clean)
        if match:
            rest = match.group(1).strip()
            if rest:
                return _split_model_reason(rest)
            capturing = True
            continue
        if capturing and clean:
            if _NEXT_SECTION_RE.match(clean):
                break
            return _split_model_reason(clean)
    raise RouterError("analyzer answer has no RECOMMENDED MODEL section")


def _split_model_reason(line: str):
    parts = re.split(r"\s+[-–—]\s+", line, maxsplit=1)
    model = strip_tier(parts[0].strip("*`\"' "))
    reason = parts[1] if len(parts) > 1 else ""
    return model, reason


class CloudAnalyzeClient(_CloudClient):
    source = "cloud-analyze"

    def _payload(self, prompt: str) -> dict:
        return {"prompt": prompt}

    def _parse(self, data: object) -> RouterResult:
        try:
            text = data["choices"][0]["message"]["content"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            raise RouterError("analyzer response has no message content")
        if not isinstance(text, str):
            raise RouterError("analyzer response content is not text")
        model, reason = parse_analyze_text(text)
        return RouterResult(_validate_model_name(model), _clean_reason(reason), None, self.source)


def build_router(settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
    if not settings.api_url:
        return MockRouter()
    if settings.api_style == "analyze":
        return CloudAnalyzeClient(settings, transport)
    return CloudRouteClient(settings, transport)
