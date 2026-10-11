"""The V2 semantic task analyst (Curvet): parsing, hedging, and failing safe."""

import asyncio
import json
import logging

import httpx
import pytest

from proxy.config import get_settings
from proxy.router_client import RouterError, build_router
from proxy.v2.analyzer import (SYSTEM_PROMPT, AnalyzerRouter, CurvetAnalyzer, MockAnalyzer, RouteApiAnalyzer,
                               build_analyzer, extract_json, parse_analysis, redact_secrets, trim_prompt)

KEY = "app_TESTKEY_not_real"
SECRET = "f" * 64
CURVET = "https://curvet.in/api/v1/chat/completions"


def analysis(tier="standard", **extra):
    data = {"task_type": "frontend_implementation", "subtype": "react login form", "complexity": "medium",
            "reasoning_requirement": "medium", "scope": "multi_file", "risk": "low", "expected_output": "medium",
            "expected_tool_steps": "several", "required_tier": tier, "confidence": 0.9,
            "same_task_probability": None, "followup_kind": "new_task", "explicit_model_request": None,
            "reason_codes": ["TASK_MEDIUM_COMPLEXITY"], "reason": "A scoped frontend feature."}
    data.update(extra)
    return data


def completion(content, model="gpt-5.4-mini"):
    return {"id": "x", "model": model, "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120}}


class FakeCurvet:
    """Answers like Curvet's chat completions; per-model behaviour is configurable."""

    def __init__(self, answers=None, status=200, delays=None, raw=None, error=None):
        self.answers = answers or {}
        self.status = status
        self.delays = delays or {}
        self.raw = raw
        self.error = error
        self.requests = []

    async def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append({"headers": dict(request.headers), "body": body})
        model = body["model"]
        if model in self.delays:
            await asyncio.sleep(self.delays[model])
        if self.error:
            raise self.error(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope " + KEY})
        if self.raw is not None:
            return httpx.Response(200, content=self.raw)
        answer = self.answers.get(model, analysis())
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return httpx.Response(200, json=completion(content, model))


def settings(**env):
    base = {"MODELMATCH_API_URL": CURVET, "MODELMATCH_API_KEY": KEY, "MODELMATCH_API_SECRET": SECRET,
            "MODELMATCH_ROUTER_MODEL": "primary", "MODELMATCH_ROUTER_FALLBACK_MODEL": "backup",
            "MODELMATCH_API_TIMEOUT": "4"}
    base.update(env)
    return get_settings(env=base)


def analyze(fake, prompt="Build the login form", **env):
    analyzer = CurvetAnalyzer(settings(**env), httpx.MockTransport(fake))
    return asyncio.run(analyzer.analyze(prompt, "claude-code"))


# -- configuration -------------------------------------------------------------


def test_curvet_style_is_detected_and_defaults_are_set():
    s = get_settings(env={"MODELMATCH_API_URL": CURVET, "MODELMATCH_API_KEY": KEY, "MODELMATCH_API_SECRET": SECRET})
    assert s.api_style == "curvet" and s.router_mode == "cloud-curvet"
    assert s.router_model and s.router_fallback_model
    assert s.api_timeout <= 10  # routing must feel instant
    assert s.api_secret == SECRET


def test_build_router_uses_the_analyst_for_v1_and_mock_when_offline():
    assert isinstance(build_router(settings()), AnalyzerRouter)
    assert build_router(get_settings(env={})).source == "mock"
    assert isinstance(build_analyzer(get_settings(env={})), MockAnalyzer)
    assert isinstance(build_analyzer(settings(MODELMATCH_API_URL="https://example.com/api/route")), RouteApiAnalyzer)


# -- tiers ----------------------------------------------------------------------


@pytest.mark.parametrize("tier, model", [("heavy", "Claude Opus 5.5"), ("standard", "Claude Sonnet 5.5"),
                                         ("light", "Claude Haiku 4.5")])
def test_api_tier_becomes_a_v1_recommendation(tier, model):
    fake = FakeCurvet({"primary": analysis(tier)})
    router = AnalyzerRouter(CurvetAnalyzer(settings(), httpx.MockTransport(fake)))
    result = asyncio.run(router.recommend("hello"))
    assert result.recommended_model == model and result.tier == tier and result.source == "curvet"
    assert result.confidence == 0.9


def test_analysis_fields_are_normalised():
    fake = FakeCurvet({"primary": analysis("heavy", task_type="Complex-Debugging", complexity="HIGH",
                                           secondary_task_type="architecture", secondary_probability=0.45,
                                           confidence=1.7, reason_codes=["COMPLEX_DEBUGGING", "made up"],
                                           explicit_model_request="  Opus ")})
    result = analyze(fake)
    assert result.task_type == "complex_debugging" and result.complexity == "high"
    assert result.secondary_task_type == "architecture" and result.ambiguous
    assert result.confidence is None  # out of range: unknown, not clamped into a fake value
    assert result.reason_codes == ("COMPLEX_DEBUGGING",)
    assert result.explicit_model_request == "opus"
    assert result.analyzer_model == "primary" and result.latency_ms is not None


def test_code_fenced_json_is_accepted():
    fake = FakeCurvet({"primary": "```json\n" + json.dumps(analysis("light")) + "\n```"})
    assert analyze(fake).required_tier == "light"


def test_request_carries_curvet_headers_and_a_redacted_trimmed_prompt():
    fake = FakeCurvet()
    prompt = "my key is sk-ant-" + "x" * 30 + " " + "word " * 4000
    analyze(fake, prompt, MODELMATCH_MAX_PROMPT_CHARS="2000")
    sent = fake.requests[0]
    assert sent["headers"]["x-app-key"] == KEY and sent["headers"]["x-app-secret"] == SECRET
    assert sent["body"]["temperature"] == 0
    assert sent["body"]["messages"][0]["content"] == SYSTEM_PROMPT
    user = json.loads(sent["body"]["messages"][1]["content"])
    assert "sk-ant-" not in user["request"] and "[REDACTED]" in user["request"]
    assert len(user["request"]) <= 2100 and user["request_chars"] == len(prompt)
    assert user["previous_task"] is None


def test_only_previous_task_metadata_is_sent():
    fake = FakeCurvet()
    analyzer = CurvetAnalyzer(settings(), httpx.MockTransport(fake))
    asyncio.run(analyzer.analyze("How should token rotation work?", "claude-code",
                                 {"task_type": "architecture", "subtype": "auth design", "minutes_ago": 2}))
    user = json.loads(fake.requests[0]["body"]["messages"][1]["content"])
    assert user["previous_task"] == {"task_type": "architecture", "subtype": "auth design", "minutes_ago": 2}


# -- failing safe -----------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_errors_raise_router_error(status):
    with pytest.raises(RouterError) as info:
        analyze(FakeCurvet(status=status))
    assert KEY not in str(info.value) and SECRET not in str(info.value)


def test_auth_failure_does_not_try_the_backup():
    fake = FakeCurvet(status=401)
    with pytest.raises(RouterError, match="rejected the ModelMatch key"):
        analyze(fake)
    assert [r["body"]["model"] for r in fake.requests] == ["primary"]


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
def test_unreachable_api_raises_router_error(error):
    with pytest.raises(RouterError):
        analyze(FakeCurvet(error=lambda request: error("down", request=request)))


def test_timeout_raises_router_error_within_budget():
    fake = FakeCurvet(delays={"primary": 5, "backup": 5})
    with pytest.raises(RouterError):
        analyze(fake, MODELMATCH_API_TIMEOUT="1.6")


@pytest.mark.parametrize("raw", [b"<html>gateway</html>", b"{}", json.dumps({"choices": []}).encode()])
def test_malformed_responses_raise_router_error(raw):
    with pytest.raises(RouterError):
        analyze(FakeCurvet(raw=raw))


@pytest.mark.parametrize("answer", ["Sure! I think this is a medium task.", json.dumps(analysis("gigantic")),
                                    json.dumps({"task_type": "debugging"})])
def test_unusable_answers_raise_router_error(answer):
    with pytest.raises(RouterError):
        analyze(FakeCurvet({"primary": answer, "backup": answer}))


def test_missing_optional_fields_become_unknown_not_guesses():
    result = analyze(FakeCurvet({"primary": {"required_tier": "light"}}))
    assert result.required_tier == "light" and result.task_type == "unknown"
    assert result.confidence is None and result.expected_output is None and result.followup_kind is None


def test_low_confidence_is_not_used_by_v1():
    fake = FakeCurvet({"primary": analysis("heavy", confidence=0.1)})
    router = AnalyzerRouter(CurvetAnalyzer(settings(), httpx.MockTransport(fake)))
    with pytest.raises(RouterError, match="unsure"):
        asyncio.run(router.recommend("hmm"))


def test_backup_runs_when_primary_fails():
    fake = FakeCurvet({"primary": "not json", "backup": analysis("light")})
    result = analyze(fake)
    assert result.required_tier == "light" and result.analyzer_model == "backup"


def test_backup_is_hedged_when_primary_is_slow():
    fake = FakeCurvet({"backup": analysis("heavy")}, delays={"primary": 3.5})
    result = analyze(fake, MODELMATCH_API_TIMEOUT="5")  # hedges after 2.5 s
    assert result.analyzer_model == "backup" and result.required_tier == "heavy"


def test_logs_never_contain_credentials(caplog):
    with caplog.at_level(logging.DEBUG):
        for status in (401, 500):
            with pytest.raises(RouterError):
                analyze(FakeCurvet(status=status))
    assert KEY not in caplog.text and SECRET not in caplog.text


# -- helpers ----------------------------------------------------------------------


def test_redaction_and_trimming_helpers():
    text = redact_secrets("password=hunter2hunter export GITHUB=ghp_" + "a" * 30 + " token: abcdefgh123")
    assert "hunter2hunter" not in text and "ghp_" not in text and "abcdefgh123" not in text
    short, trimmed = trim_prompt("x" * 100, 200)
    assert short == "x" * 100 and not trimmed
    long_text, trimmed = trim_prompt("a" * 5000 + "THE ASK", 1000)
    assert trimmed and long_text.endswith("THE ASK") and len(long_text) <= 1100


def test_extract_json_and_parse_model_name_fallback():
    assert extract_json('noise {"a": 1} noise') == {"a": 1}
    parsed = parse_analysis({"recommended_model": "Claude Opus 5.5"}, "route")
    assert parsed.required_tier == "heavy" and parsed.task_type == "unknown"


def test_route_api_analyzer_accepts_v1_contract():
    def answer(request):
        assert json.loads(request.content)["client"] == "claude-code"
        return httpx.Response(200, json={"recommended_model": "claude-haiku-4-5", "reason": "tiny", "confidence": 0.8})

    s = settings(MODELMATCH_API_URL="https://example.com/api/route")
    result = asyncio.run(RouteApiAnalyzer(s, httpx.MockTransport(answer)).analyze("hi"))
    assert result.required_tier == "light" and result.source == "route"


def test_mock_analyzer_is_low_confidence():
    result = asyncio.run(MockAnalyzer().analyze("Design the system architecture for a new payments platform"))
    assert result.required_tier == "heavy" and result.confidence <= 0.5 and result.source == "mock"
