"""The semantic task analyst (System 1).

It reads the developer's prompt and says what the task really needs: its type,
complexity, risk, and the *capability tier* (light / standard / heavy) that will
reliably get it right. It never names the model to run: which model provides a
tier is decided per tool (adapters.py), so task classification and model choice
stay separate.

* CurvetAnalyzer    - production: one small, fast model behind the Curvet AI gateway
                      (OpenAI-style chat completions), answering a strict JSON schema.
* RouteApiAnalyzer  - a hosted ModelMatch /api/route that returns the same schema
                      (or the older {"recommended_model": ...} contract).
* MockAnalyzer      - offline / tests only: the V1 keyword heuristics, low confidence.

Every failure raises RouterError; callers then keep the current model (fail open).
The prompt is trimmed and obvious secrets are redacted before it leaves the machine.
"""

import asyncio
import json
import re
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from ..config import Settings
from ..router_client import MockRouter, RouterError, RouterResult, _clean_reason
from .schema import FOLLOWUPS, LEVELS, OUTPUT_SIZES, SCOPES, TASK_TYPES, TIERS, TOOL_STEPS, TaskAnalysis

ANALYSIS_SCHEMA = "modelmatch.task_analysis.v1"
USER_AGENT = "modelmatch-proxy/2"
MIN_RETRY_SECONDS = 1.5
HEDGE_AFTER_SECONDS = 3.0  # typical answers take ~2-2.5 s (measured 2026-10-08)
_ALLOWED_REASON_CODES = (
    "TASK_LOW_COMPLEXITY", "TASK_MEDIUM_COMPLEXITY", "TASK_HIGH_COMPLEXITY", "ARCHITECTURE_REASONING",
    "COMPLEX_DEBUGGING", "HIGH_RISK", "AMBIGUOUS_TASK", "CROSS_SYSTEM", "SMALL_SCOPE", "MULTI_STEP",
)

SYSTEM_PROMPT = """You are the task analyst inside ModelMatch, a tool that picks which AI model a coding agent \
(Claude Code, Codex CLI or GitHub Copilot CLI) should use for a developer's next request.

Read the developer's request and judge what it really demands. Do NOT answer it or carry it out.
Judge by meaning, not keywords: "fix a bug" can be trivial (a typo) or very hard (an intermittent race \
condition across services). Consider what is at stake (payments, authentication, security and data loss raise \
the bar), how much uncertainty and investigation is involved, how many parts of the system interact, and how \
much careful multi-step reasoning a correct answer needs.

Capability tiers. Pick the CHEAPEST tier that will reliably get it right:
- "light": quick lookups, running or explaining a simple command, small well-specified edits (rename, change a \
value, fix a typo, tweak a style), short factual answers, simple docs or comments.
- "standard": ordinary implementation and debugging with a clear scope: build a component or endpoint, write \
tests, refactor within a module, review code, explain how code works, routine devops.
- "heavy": architecture and system design, cross-system or concurrency debugging (race conditions, retries, \
distributed state), high-consequence or security-sensitive changes, large migrations or refactors across many \
files, ambiguous problems that need deep investigation.

If "previous_task" is given, it describes the developer's previous request in this same session. Estimate \
"same_task_probability": how likely this request continues that same piece of work IN THE SAME PHASE (a \
follow-up question about the same design, the next step of the same fix, a correction). Moving to a different \
phase or kind of work (design -> implementation, implementation -> deployment, frontend -> backend) or to a \
different component is a NEW task, even within the same project or feature.
"followup_kind": "new_task" (different work or a new phase, or no previous task), "continuation" (follow-up in \
the same phase), "retry_or_correction" (the previous answer was wrong or didn't work: "still failing", "that \
broke X", "try again"), "escalation_request" (the developer asks for more depth or a stronger model).
"explicit_model_request": only if the developer explicitly asks to use a particular model for this request \
("use opus for this", "switch to haiku"), give that name in lowercase ("opus", "sonnet", "haiku", "fable", \
"gpt-5.5"...); otherwise null. Merely mentioning a model is not a request.

Reply with ONLY one JSON object (no prose, no code fences) with exactly these keys:
{"task_type": one of [TASK_TYPES],
 "subtype": "at most 6 generic words naming the task",
 "secondary_task_type": one of the same list or null,
 "secondary_probability": 0-1 or null (how plausible the secondary reading is),
 "complexity": "low"|"medium"|"high",
 "reasoning_requirement": "low"|"medium"|"high",
 "scope": "none"|"single_file"|"multi_file"|"cross_system"|"unknown",
 "risk": "low"|"medium"|"high",
 "expected_output": "short"|"medium"|"long",
 "expected_tool_steps": "few"|"several"|"many",
 "required_tier": "light"|"standard"|"heavy",
 "confidence": 0-1 (how sure you are about required_tier),
 "same_task_probability": 0-1, or null when there is no previous task,
 "followup_kind": "new_task"|"continuation"|"retry_or_correction"|"escalation_request",
 "explicit_model_request": string or null,
 "reason_codes": up to 4 of [REASON_CODES],
 "reason": "one short sentence, at most 20 words, useful to the developer"}""".replace(
    "[TASK_TYPES]", "[" + ", ".join(t for t in TASK_TYPES) + "]").replace(
    "[REASON_CODES]", "[" + ", ".join(_ALLOWED_REASON_CODES) + "]")


# ---------------------------------------------------------------------------
# What leaves the machine: trimmed, redacted
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bapp_[0-9a-f]{16,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\b[0-9a-f]{48,}\b"),
]
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b((?:api[_-]?key|secret|token|password|passwd|pwd|auth|authorization|access[_-]?key|private[_-]?key)"
    r"\w*\s*[:=]\s*[\"']?)([^\s\"']{6,})")


def redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _ASSIGNED_SECRET.sub(lambda m: m.group(1) + "[REDACTED]", text)


def trim_prompt(prompt: str, max_chars: int) -> Tuple[str, bool]:
    """Keep the start and end of very long prompts (that is where the ask usually is)."""
    if max_chars <= 0 or len(prompt) <= max_chars:
        return prompt, False
    head = int(max_chars * 0.7)
    tail = max(0, max_chars - head - 60)
    omitted = len(prompt) - head - tail
    text = prompt[:head] + "\n\n[... {} characters omitted ...]\n\n".format(omitted)
    if tail:
        text += prompt[-tail:]
    return text, True


def scrub(text: str, *secrets: str) -> str:
    """Remove our own credentials from any text we might log (e.g. an echoed error)."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


# ---------------------------------------------------------------------------
# Parsing the analyst's answer
# ---------------------------------------------------------------------------

_FAMILY_TIER = {"haiku": "light", "sonnet": "standard", "opus": "heavy", "fable": "heavy"}


def _enum(value: object, allowed, default: Optional[str] = None) -> Optional[str]:
    if not isinstance(value, str):
        return default
    value = value.strip().lower().replace("-", "_").replace(" ", "_")
    return value if value in allowed else default


def _prob(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return round(value, 3) if 0.0 <= value <= 1.0 else None


def _words(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    words = re.sub(r"[^\w\s/+.-]", " ", value).split()
    return " ".join(words[:limit])[:60]


def extract_json(text: str) -> Dict[str, Any]:
    """The first JSON object in a model's answer (tolerates code fences and stray prose)."""
    if not isinstance(text, str) or not text.strip():
        raise RouterError("task analyst returned an empty answer")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RouterError("task analyst answer has no JSON object")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        raise RouterError("task analyst answer is not valid JSON")
    if not isinstance(data, dict):
        raise RouterError("task analyst answer is not a JSON object")
    return data


def parse_analysis(data: Dict[str, Any], source: str, analyzer_model: str = "",
                   latency_ms: Optional[int] = None) -> TaskAnalysis:
    tier = _enum(data.get("required_tier"), TIERS)
    if tier is None:
        # Tolerate an answer that names a model instead of a tier ("opus", "Claude Sonnet 5.5").
        named = str(data.get("required_tier") or data.get("recommended_model") or "").lower()
        tier = next((t for family, t in _FAMILY_TIER.items() if family in named), None)
    if tier is None:
        raise RouterError("task analyst gave no usable capability tier")

    task_type = _enum(data.get("task_type"), TASK_TYPES, "unknown")
    secondary = _enum(data.get("secondary_task_type"), TASK_TYPES)
    explicit = data.get("explicit_model_request")
    explicit = " ".join(explicit.lower().split())[:40] if isinstance(explicit, str) and explicit.strip() else None
    if explicit in ("null", "none", "no", "false"):
        explicit = None
    codes = data.get("reason_codes")
    codes = tuple(c for c in codes if isinstance(c, str) and c in _ALLOWED_REASON_CODES)[:4] \
        if isinstance(codes, list) else ()
    return TaskAnalysis(
        task_type=task_type,
        subtype=_words(data.get("subtype"), 6),
        secondary_task_type=secondary if secondary != task_type else None,
        secondary_probability=_prob(data.get("secondary_probability")) if secondary and secondary != task_type
        else None,
        complexity=_enum(data.get("complexity"), LEVELS, "medium"),
        reasoning_requirement=_enum(data.get("reasoning_requirement"), LEVELS, "medium"),
        scope=_enum(data.get("scope"), SCOPES, "unknown"),
        risk=_enum(data.get("risk"), LEVELS, "unknown"),
        expected_output=_enum(data.get("expected_output"), OUTPUT_SIZES),
        expected_tool_steps=_enum(data.get("expected_tool_steps"), TOOL_STEPS),
        required_tier=tier,
        confidence=_prob(data.get("confidence")),
        same_task_probability=_prob(data.get("same_task_probability")),
        followup_kind=_enum(data.get("followup_kind"), FOLLOWUPS),
        explicit_model_request=explicit,
        reason_codes=codes,
        reason=_clean_reason(data.get("reason"))[:200],
        source=source,
        analyzer_model=analyzer_model,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Analysts
# ---------------------------------------------------------------------------


def _user_message(prompt: str, client: str, max_chars: int, previous_task: Optional[Dict[str, Any]]) -> str:
    text, trimmed = trim_prompt(redact_secrets(prompt), max_chars)
    payload: Dict[str, Any] = {"client": client, "request": text, "request_chars": len(prompt)}
    if trimmed:
        payload["note"] = "the request was shortened; judge the task from what is shown"
    payload["previous_task"] = previous_task or None
    return json.dumps(payload, ensure_ascii=False)


class TaskAnalyzer:
    source = "base"

    async def analyze(self, prompt: str, client: str,
                      previous_task: Optional[Dict[str, Any]] = None) -> TaskAnalysis:
        raise NotImplementedError


class CurvetAnalyzer(TaskAnalyzer):
    """One small model behind the Curvet gateway, answering the strict JSON schema above."""

    source = "curvet"

    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.url = settings.api_url
        self.app_key = settings.api_key
        self.app_secret = settings.api_secret
        self.timeout = settings.api_timeout
        self.max_prompt_chars = settings.max_prompt_chars
        self.models = [m for m in (settings.router_model, settings.router_fallback_model) if m and m != "-"]
        self.hedge_after = min(HEDGE_AFTER_SECONDS, self.timeout / 2)
        self.transport = transport

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.app_key:
            headers["x-app-key"] = self.app_key
            headers["Authorization"] = "Bearer " + self.app_key
        if self.app_secret:
            headers["x-app-secret"] = self.app_secret
        return headers

    def _body(self, model: str, user_message: str) -> Dict[str, Any]:
        return {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message}],
            "temperature": 0,
            "max_tokens": 600,
        }

    async def _call(self, client: httpx.AsyncClient, model: str, user_message: str, timeout: float) -> TaskAnalysis:
        started = time.monotonic()
        try:
            response = await client.post(self.url, json=self._body(model, user_message), headers=self._headers(),
                                         timeout=timeout)
        except httpx.TimeoutException:
            raise RouterError("task analyst timed out after {:.1f}s".format(timeout))
        except httpx.HTTPError as exc:
            raise RouterError("could not reach the task analyst ({})".format(type(exc).__name__))
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code in (401, 403):
            raise _AuthError("task analyst rejected the ModelMatch key (HTTP {})".format(response.status_code))
        if response.status_code != 200:
            raise RouterError("task analyst returned HTTP {}".format(response.status_code))
        try:
            data = response.json()
        except ValueError:
            raise RouterError("task analyst returned a non-JSON response")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RouterError("task analyst response has no message content")
        return parse_analysis(extract_json(content), self.source, str(data.get("model") or model), latency_ms)

    async def analyze(self, prompt: str, client: str = "claude-code",
                      previous_task: Optional[Dict[str, Any]] = None) -> TaskAnalysis:
        """Ask the primary model; if it is slow (or fails), the fallback runs too and the first good answer wins."""
        if not self.models:
            raise RouterError("no task analyst model configured (MODELMATCH_ROUTER_MODEL)")
        user_message = _user_message(prompt, client, self.max_prompt_chars, previous_task)
        deadline = time.monotonic() + self.timeout
        errors = []
        async with httpx.AsyncClient(transport=self.transport) as http:
            def start(model: str) -> "asyncio.Future":
                return asyncio.ensure_future(self._call(http, model, user_message,
                                                        max(0.5, deadline - time.monotonic())))

            started = deadline - self.timeout
            pending = {start(self.models[0])}
            backups = list(self.models[1:])
            try:
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    wait = remaining
                    if backups:  # wake up when it is time to start the backup
                        wait = min(remaining, max(0.0, started + self.hedge_after - time.monotonic()))
                    done, pending = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        try:
                            return task.result()
                        except _AuthError as exc:  # same key for every model: no point trying another
                            raise RouterError(scrub(str(exc), self.app_key, self.app_secret))
                        except RouterError as exc:
                            errors.append(scrub(str(exc), self.app_key, self.app_secret))
                    # The primary failed, or is slower than hedge_after: start the backup alongside it.
                    if backups and (done or time.monotonic() - started >= self.hedge_after):
                        backup = backups.pop(0)
                        if deadline - time.monotonic() >= MIN_RETRY_SECONDS:
                            pending.add(start(backup))
            finally:
                for task in pending:
                    task.cancel()
        if not errors:
            errors.append("task analyst timed out after {:g}s".format(self.timeout))
        raise RouterError("; ".join(errors))


class _AuthError(RouterError):
    pass


class RouteApiAnalyzer(TaskAnalyzer):
    """A hosted /api/route. Accepts the V2 analysis schema, or the V1 {"recommended_model": ...} answer."""

    source = "route"

    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.url = settings.api_url
        self.api_key = settings.api_key
        self.timeout = settings.api_timeout
        self.max_prompt_chars = settings.max_prompt_chars
        self.transport = transport

    async def analyze(self, prompt: str, client: str = "claude-code",
                      previous_task: Optional[Dict[str, Any]] = None) -> TaskAnalysis:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        text, _ = trim_prompt(redact_secrets(prompt), self.max_prompt_chars)
        payload = {"schema": ANALYSIS_SCHEMA, "prompt": text, "client": client, "previous_task": previous_task}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as http:
                response = await http.post(self.url, json=payload, headers=headers)
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
        if not isinstance(data, dict):
            raise RouterError("response is not a JSON object")
        latency_ms = int((time.monotonic() - started) * 1000)
        if "required_tier" not in data and "recommended_model" in data:
            data = dict(data, task_type=data.get("task_type") or "unknown")
        return parse_analysis(data, self.source, latency_ms=latency_ms)


class MockAnalyzer(TaskAnalyzer):
    """Offline stand-in (no API configured, and tests). Keyword heuristics, so its confidence is kept low."""

    source = "mock"
    _TIER_OF = {"Claude Haiku 4.5": ("light", "small_edit", "low"),
                "Claude Sonnet 5.5": ("standard", "backend_implementation", "medium"),
                "Claude Opus 5.5": ("heavy", "architecture", "high")}

    def __init__(self):
        self.router = MockRouter()

    async def analyze(self, prompt: str, client: str = "claude-code",
                      previous_task: Optional[Dict[str, Any]] = None) -> TaskAnalysis:
        result = await self.router.recommend(prompt)
        tier, task_type, level = self._TIER_OF.get(result.recommended_model, ("standard", "unknown", "medium"))
        lower = result.recommended_model.lower()
        tier = next((t for family, t in _FAMILY_TIER.items() if family in lower), tier)
        return TaskAnalysis(task_type=task_type, complexity=level, reasoning_requirement=level, required_tier=tier,
                            confidence=min(0.5, result.confidence or 0.5), reason=result.reason, source=self.source,
                            followup_kind="new_task" if previous_task is None else None)


def build_analyzer(settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None) -> TaskAnalyzer:
    if not settings.api_url:
        return MockAnalyzer()
    if settings.api_style == "curvet":
        return CurvetAnalyzer(settings, transport)
    return RouteApiAnalyzer(settings, transport)


# ---------------------------------------------------------------------------
# V1 compatibility: the analyst as the V1 router's brain
# ---------------------------------------------------------------------------

# The tier names V1 already understands (the agents proxy maps them to its own models by size).
TIER_MODEL_NAMES = {"light": "Claude Haiku 4.5", "standard": "Claude Sonnet 5.5", "heavy": "Claude Opus 5.5"}
MIN_V1_CONFIDENCE = 0.3


class AnalyzerRouter:
    """Makes a TaskAnalyzer look like a V1 router: one model for the prompt, plus its tier."""

    def __init__(self, analyzer: TaskAnalyzer, client: str = "claude-code",
                 tier_models: Optional[Dict[str, str]] = None):
        self.analyzer = analyzer
        self.client = client
        self.tier_models = dict(TIER_MODEL_NAMES, **(tier_models or {}))
        self.source = analyzer.source

    async def recommend(self, prompt: str) -> RouterResult:
        analysis = await self.analyzer.analyze(prompt, self.client)
        if analysis.confidence is not None and analysis.confidence < MIN_V1_CONFIDENCE:
            raise RouterError("task analyst was unsure (confidence {:.2f})".format(analysis.confidence))
        reason = analysis.reason or "Matched to a {} task.".format(analysis.task_type.replace("_", " "))
        return RouterResult(self.tier_models[analysis.required_tier], reason, analysis.confidence,
                            analysis.source, tier=analysis.required_tier)
