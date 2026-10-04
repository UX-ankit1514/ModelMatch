"""The agents proxy: endpoints, Codex's per-turn model switching, and untouched passthrough."""

import gzip
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.agents.app import create_app
from proxy.agents.config import get_agent_settings

SESSION = "01a10257-4d46-7ad2-9100-cbf0ebd364be"
TURN = "01a10257-4d93-7002-bf4e-fff3224a1d43"
CATALOG = {"models": [
    {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "description": "Older coding model for complex work.",
     "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")],
     "service_tiers": [{"id": "priority"}], "support_verbosity": True},
    {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "description": "Older balanced model."},
    {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "description": "Older fast and efficient model.",
     "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium")], "service_tiers": [],
     "support_verbosity": False},
]}


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def streamed(status, data, content_type="application/json"):
    if not isinstance(data, bytes):
        data = json.dumps(data).encode()
    return httpx.Response(status, headers={"content-type": content_type}, stream=_Stream(data))


class FakeUpstream:
    """Records what the proxy forwards to Codex's model API and answers like it."""

    def __init__(self, reject_models=(), fail=False):
        self.requests = []
        self.reject_models = set(reject_models)
        self.fail = fail

    def __call__(self, request):
        if self.fail:
            raise httpx.ConnectError("no internet", request=request)
        raw = request.content
        body = None
        if raw and not request.headers.get("content-encoding"):
            body = json.loads(raw)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body, "raw": raw})
        if body and body.get("model") in self.reject_models:
            return streamed(400, {"error": {"message": "unsupported for this model", "type": "invalid_request_error"}})
        sse = b"event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"
        return streamed(200, sse, "text/event-stream")


@pytest.fixture
def codex_home(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    (home / "models_cache.json").write_text(json.dumps(CATALOG))
    return home


def make_client(tmp_path, codex_home, upstream=None, **env):
    base = {"MODELMATCH_LOG_DIR": str(tmp_path / "logs"),
            "MODELMATCH_AGENTS_STATE_FILE": str(tmp_path / "state" / "agents.json"),
            "MODELMATCH_CODEX_HOME": str(codex_home)}
    base.update(env)
    settings = get_agent_settings(env=base, root=tmp_path)
    upstream = upstream or FakeUpstream()
    app = create_app(settings, upstream_transport=httpx.MockTransport(upstream))
    return TestClient(app), upstream


def accept(client, prompt="What is the capital of France?", turn=TURN, accepted=True, current="gpt-5.6-sol"):
    client.post("/session/start", json={"client": "codex", "session_id": SESSION, "source": "startup",
                                        "model": current})
    rec = client.post("/recommend", json={"client": "codex", "session_id": SESSION, "turn_id": turn,
                                          "prompt": prompt, "current_model": current}).json()
    sel = client.post("/selection", json={"client": "codex", "session_id": SESSION,
                                           "recommendation_id": rec["recommendation_id"], "accepted": accepted,
                                           "decided_by": "test"}).json()
    return rec, sel


def responses_request(model="gpt-5.6-sol", effort="high", tier="priority", verbosity="low"):
    body = {"model": model, "stream": True, "store": False, "input": [{"type": "message", "role": "user"}],
            "reasoning": {"effort": effort}, "text": {"verbosity": verbosity}, "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": SESSION, "client_metadata": {"session_id": SESSION, "turn_id": TURN}}
    if tier:
        body["service_tier"] = tier
    return body


def codex_headers(turn=TURN, kind="turn", **extra):
    meta = {"session_id": SESSION, "thread_id": SESSION, "turn_id": turn, "request_kind": kind}
    headers = {"session-id": SESSION, "x-codex-turn-metadata": json.dumps(meta), "authorization": "Bearer sk-secret",
               "originator": "codex_cli_rs"}
    headers.update(extra)
    return headers


# -- endpoints ------------------------------------------------------------------------


def test_health_and_models(tmp_path, codex_home):
    client, _ = make_client(tmp_path, codex_home)
    with client:
        health = client.get("/health").json()
        assert health["service"] == "modelmatch-agents-proxy" and health["router"] == "mock"
        models = client.get("/models/codex").json()
        assert models["models"] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        assert models["tiers"] == {"light": "gpt-5.6-luna", "standard": "gpt-5.6-terra", "heavy": "gpt-5.6-sol"}
        assert client.get("/models/nope").status_code == 400


def test_codex_recommendation_maps_to_codex_models(tmp_path, codex_home):
    client, _ = make_client(tmp_path, codex_home)
    with client:
        rec, sel = accept(client)
        assert rec["recommended_model"] == "GPT-5.6-Luna" and rec["model_id"] == "gpt-5.6-luna"
        assert rec["routable"] and not rec["same_as_current"]
        assert sel == {"ok": True, "applied": True, "active_model": "gpt-5.6-luna"}
        heavy = client.post("/recommend", json={"client": "codex", "session_id": SESSION, "turn_id": "t2",
                                                "current_model": "gpt-5.6-sol",
                                                "prompt": "Design a system architecture for the migration with "
                                                          "trade-offs"}).json()
        assert heavy["model_id"] == "gpt-5.6-sol" and heavy["same_as_current"]


def test_tier_pins_from_env(tmp_path, codex_home):
    client, _ = make_client(tmp_path, codex_home, MODELMATCH_CODEX_LIGHT_MODEL="gpt-5.6-terra")
    with client:
        rec, _ = accept(client)
        assert rec["model_id"] == "gpt-5.6-terra"


def test_copilot_recommendation_uses_the_offered_models(tmp_path, codex_home):
    client, _ = make_client(tmp_path, codex_home)
    offered = [{"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6", "category": "versatile"},
               {"id": "claude-haiku-4.5", "name": "Claude Haiku 4.5", "category": "lightweight"}]
    with client:
        rec = client.post("/recommend", json={"client": "copilot", "session_id": "c-1", "prompt": "hi there",
                                              "current_model": "claude-sonnet-4.6",
                                              "available_models": offered}).json()
        assert rec["model_id"] == "claude-haiku-4.5" and rec["recommended_model"] == "Claude Haiku 4.5"
        none = client.post("/recommend", json={"client": "copilot", "session_id": "c-1", "prompt": "hi"}).json()
        assert not none["routable"] and "Couldn't read your Copilot plan" in none["routing_note"]
        other = client.post("/recommend", json={"client": "copilot", "session_id": "c-1", "prompt": "wc-test: Grok 9",
                                                "available_models": offered}).json()
        assert not other["routable"] and other["routing_note"] == "Grok 9 isn't in your Copilot plan."
        assert client.get("/session/copilot/c-1").json()["session"]["turn"]["recommended_model"] == "Grok 9"


def test_session_start_does_not_undo_a_racing_first_prompt(tmp_path, codex_home):
    client, _ = make_client(tmp_path, codex_home)
    with client:
        rec = client.post("/recommend", json={"client": "codex", "session_id": SESSION, "turn_id": TURN,
                                              "prompt": "hi", "current_model": "gpt-5.6-sol"}).json()
        client.post("/selection", json={"client": "codex", "session_id": SESSION,
                                        "recommendation_id": rec["recommendation_id"], "accepted": True})
        client.post("/session/start", json={"client": "codex", "session_id": SESSION, "source": "startup"})
        assert client.get("/session/codex/" + SESSION).json()["session"]["active_route"] == "gpt-5.6-luna"
        client.post("/session/start", json={"client": "codex", "session_id": SESSION, "source": "clear"})
        assert client.get("/session/codex/" + SESSION).json()["session"]["active_route"] is None


# -- Codex passthrough --------------------------------------------------------------------


def test_untouched_without_a_route(tmp_path, codex_home):
    client, upstream = make_client(tmp_path, codex_home)
    raw = json.dumps(responses_request()).encode()
    with client:
        response = client.post("/v1/responses", content=raw, headers=codex_headers())
    assert response.status_code == 200 and "response.completed" in response.text
    sent = upstream.requests[0]
    assert sent["raw"] == raw  # byte for byte
    assert sent["url"] == "https://api.openai.com/v1/responses"
    assert sent["headers"]["authorization"] == "Bearer sk-secret"
    assert "x-modelmatch-routed-model" not in response.headers


def test_accepted_turn_is_routed_and_adapted(tmp_path, codex_home):
    client, upstream = make_client(tmp_path, codex_home)
    with client:
        accept(client)
        response = client.post("/v1/responses", json=responses_request(), headers=codex_headers())
        assert response.headers["x-modelmatch-routed-model"] == "gpt-5.6-luna"
    body = upstream.requests[0]["body"]
    assert body["model"] == "gpt-5.6-luna"
    assert body["reasoning"] == {"effort": "medium"}  # high isn't offered by luna: nearest lower level
    assert "service_tier" not in body  # luna has no fast mode
    assert "text" not in body  # luna doesn't take verbosity
    assert body["input"] == responses_request()["input"] and body["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize("headers", [
    codex_headers(turn="a-later-turn"),  # the choice was for one prompt
    codex_headers(kind="prewarm"),
    codex_headers(kind="compaction"),
    {"session-id": "another-session"},  # e.g. a subagent
])
def test_other_requests_keep_their_model(tmp_path, codex_home, headers):
    client, upstream = make_client(tmp_path, codex_home)
    with client:
        accept(client)
        client.post("/v1/responses", json=responses_request(), headers=headers)
    assert upstream.requests[0]["body"]["model"] == "gpt-5.6-sol"


def test_rejected_choice_keeps_the_model(tmp_path, codex_home):
    client, upstream = make_client(tmp_path, codex_home)
    with client:
        accept(client, accepted=False)
        client.post("/v1/responses", json=responses_request(), headers=codex_headers())
    assert upstream.requests[0]["body"]["model"] == "gpt-5.6-sol"


def test_rejected_route_is_retried_unchanged(tmp_path, codex_home):
    client, upstream = make_client(tmp_path, codex_home, upstream=FakeUpstream(reject_models={"gpt-5.6-luna"}))
    raw = json.dumps(responses_request()).encode()
    with client:
        accept(client)
        response = client.post("/v1/responses", content=raw, headers=codex_headers())
        assert response.status_code == 200
        state = client.get("/session/codex/" + SESSION).json()["session"]
    assert [r["body"]["model"] for r in upstream.requests] == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert upstream.requests[1]["raw"] == raw
    assert state["route_status"] == "failed"
    assert "retrying with gpt-5.6-sol" in (tmp_path / "logs" / "agents-proxy.log").read_text()


def test_gzip_body_is_read_and_routed(tmp_path, codex_home):
    client, upstream = make_client(tmp_path, codex_home)
    with client:
        accept(client)
        client.post("/v1/responses", content=gzip.compress(json.dumps(responses_request()).encode()),
                    headers=codex_headers(**{"content-encoding": "gzip", "content-type": "application/json"}))
    sent = upstream.requests[0]
    assert "content-encoding" not in sent["headers"]
    assert json.loads(sent["raw"])["model"] == "gpt-5.6-luna"


def test_zstd_body_is_read_and_routed(tmp_path, codex_home):
    zstandard = pytest.importorskip("zstandard")
    client, upstream = make_client(tmp_path, codex_home)
    with client:
        accept(client)
        client.post("/v1/responses", content=zstandard.ZstdCompressor(level=3).compress(
            json.dumps(responses_request()).encode()),
            headers=codex_headers(**{"content-encoding": "zstd", "content-type": "application/json"}))
    assert json.loads(upstream.requests[0]["raw"])["model"] == "gpt-5.6-luna"


def test_unreadable_body_passes_through_untouched(tmp_path, codex_home):
    client, upstream = make_client(tmp_path, codex_home)
    with client:
        accept(client)
        client.post("/v1/responses", content=b"\x28\xb5\x2f\xfd-not-really-zstd",
                    headers=codex_headers(**{"content-encoding": "br"}))
    sent = upstream.requests[0]
    assert sent["raw"] == b"\x28\xb5\x2f\xfd-not-really-zstd" and sent["headers"]["content-encoding"] == "br"


def test_websocket_upgrade_is_refused_with_426(tmp_path, codex_home):
    client, upstream = make_client(tmp_path, codex_home)
    with client:
        response = client.get("/v1/responses", headers={"connection": "Upgrade", "upgrade": "websocket"})
    assert response.status_code == 426 and upstream.requests == []


def test_upstream_choice(tmp_path, codex_home):
    client, upstream = make_client(tmp_path, codex_home)
    with client:
        client.get("/v1/models?client_version=0.159.2", headers={"chatgpt-account-id": "acct"})
    assert upstream.requests[0]["url"] == "https://chatgpt.com/backend-api/codex/models?client_version=0.159.2"

    chained, upstream2 = make_client(tmp_path, codex_home,
                                     MODELMATCH_CODEX_UPSTREAM_URL="http://127.0.0.1:10100/v1/")
    with chained:
        chained.post("/v1/responses", json=responses_request(), headers=codex_headers())
    assert upstream2.requests[0]["url"] == "http://127.0.0.1:10100/v1/responses"


def test_upstream_down_is_a_clean_error(tmp_path, codex_home):
    client, _ = make_client(tmp_path, codex_home, upstream=FakeUpstream(fail=True))
    with client:
        response = client.post("/v1/responses", json=responses_request(), headers=codex_headers())
    assert response.status_code == 502 and "could not reach" in response.json()["error"]["message"]


def test_logs_never_contain_prompts_or_keys(tmp_path, codex_home):
    client, _ = make_client(tmp_path, codex_home)
    with client:
        accept(client, prompt="Secret codename BLUEBIRD")
        client.post("/v1/responses", json=responses_request(), headers=codex_headers())
    log = (tmp_path / "logs" / "agents-proxy.log").read_text()
    assert "BLUEBIRD" not in log and "sk-secret" not in log
    assert "routing | codex" in log
