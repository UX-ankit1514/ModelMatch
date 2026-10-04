import asyncio
import json

import httpx
import pytest

from proxy.config import get_settings
from proxy.router_client import (CloudAnalyzeClient, CloudRouteClient, MockRouter, RouterError, build_router,
                                 parse_analyze_text)


def run(coro):
    return asyncio.run(coro)


def settings(tmp_path, **env):
    base = {"MODELMATCH_LOG_DIR": str(tmp_path / "logs"),
            "MODELMATCH_STATE_FILE": str(tmp_path / "state.json")}
    base.update(env)
    return get_settings(env=base, root=tmp_path)


# -- mock ---------------------------------------------------------------------


@pytest.mark.parametrize("prompt, expected", [
    ("What is the capital of France?", "Claude Haiku 4.5"),
    ("Fix this bug in my python function that throws a KeyError", "Claude Sonnet 5.5"),
    ("Design a system architecture for a multi-region payments platform with trade-offs", "Claude Opus 5.5"),
    ("word " * 450, "Claude Opus 5.5"),
    ("hello wc-test: GPT-4o", "GPT-4o"),
])
def test_mock_router(prompt, expected):
    result = run(MockRouter().recommend(prompt))
    assert result.recommended_model == expected
    assert result.reason
    assert result.source == "mock"


def test_build_router_picks_backend(tmp_path):
    assert isinstance(build_router(settings(tmp_path)), MockRouter)
    assert isinstance(build_router(settings(tmp_path, MODELMATCH_API_URL="https://x.test/api/route")),
                      CloudRouteClient)
    assert isinstance(build_router(settings(tmp_path, MODELMATCH_API_URL="https://x.test/api/analyze")),
                      CloudAnalyzeClient)


# -- cloud /api/route contract -------------------------------------------------


def route_client(tmp_path, handler, **env):
    env.setdefault("MODELMATCH_API_URL", "https://wc.test/api/route")
    return CloudRouteClient(settings(tmp_path, **env), transport=httpx.MockTransport(handler))


def test_cloud_route_success_sends_minimum_data(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"recommended_model": "Claude Sonnet 5.5", "reason": " Good  at code. ",
                                         "confidence": 0.91})

    client = route_client(tmp_path, handler, MODELMATCH_MAX_PROMPT_CHARS="10",
                          MODELMATCH_API_KEY="secret-test-key")
    result = run(client.recommend("a" * 50))
    assert result.recommended_model == "Claude Sonnet 5.5"
    assert result.reason == "Good at code."
    assert result.confidence == 0.91
    assert seen["body"] == {"prompt": "a" * 10, "client": "claude-code"}
    assert seen["auth"] == "Bearer secret-test-key"


@pytest.mark.parametrize("response", [
    httpx.Response(500, json={"error": "boom"}),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json={"reason": "no model"}),
    httpx.Response(200, json={"recommended_model": ""}),
    httpx.Response(200, json=["not", "an", "object"]),
    httpx.Response(200, json={"recommended_model": "x" * 500}),
])
def test_cloud_route_malformed_responses_raise(tmp_path, response):
    with pytest.raises(RouterError):
        run(route_client(tmp_path, lambda request: response).recommend("hi"))


def test_cloud_route_bad_confidence_is_dropped(tmp_path):
    client = route_client(tmp_path, lambda r: httpx.Response(200, json={"recommended_model": "GPT-4o",
                                                                         "confidence": 7}))
    assert run(client.recommend("hi")).confidence is None


def test_cloud_route_timeout_and_network_errors_raise(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(RouterError, match="timed out"):
        run(route_client(tmp_path, timeout).recommend("hi"))
    with pytest.raises(RouterError, match="could not reach"):
        run(route_client(tmp_path, refused).recommend("hi"))


# -- existing /api/analyze adapter -------------------------------------------

ANALYZE_TEXT = """SECTION 1: RECOMMENDED MODEL
Gemini 3.5 Flash low - This is a simple question, so it will burn few tokens and give a fast response.

SECTION 2: TOKEN RISK
Low - short prompt.

SECTION 3: WORKFLOW ADVICE
1. Be specific.

SECTION 4: OPTIMIZED PROMPT
What is the capital of France?"""


def test_parse_analyze_text_variants():
    assert parse_analyze_text(ANALYZE_TEXT) == (
        "Gemini 3.5 Flash", "This is a simple question, so it will burn few tokens and give a fast response.")
    assert parse_analyze_text("**SECTION 1: RECOMMENDED MODEL:** Claude 4 Opus max - deep reasoning")[0] == \
        "Claude 4 Opus"
    assert parse_analyze_text("RECOMMENDED MODEL\n\n**GPT-4o medium** – balanced")[0] == "GPT-4o"
    with pytest.raises(RouterError):
        parse_analyze_text("SECTION 2: TOKEN RISK\nLow")


def test_cloud_analyze_client(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": ANALYZE_TEXT}}]})

    client = CloudAnalyzeClient(settings(tmp_path, MODELMATCH_API_URL="https://wc.test/api/analyze"),
                                transport=httpx.MockTransport(handler))
    result = run(client.recommend("What is the capital of France?"))
    assert result.recommended_model == "Gemini 3.5 Flash"
    assert result.confidence is None
    assert seen["body"] == {"prompt": "What is the capital of France?"}

    broken = CloudAnalyzeClient(settings(tmp_path, MODELMATCH_API_URL="https://wc.test/api/analyze"),
                                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []})))
    with pytest.raises(RouterError):
        run(broken.recommend("hi"))
