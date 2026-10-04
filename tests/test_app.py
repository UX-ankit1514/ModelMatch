import json

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app, extract_session_id
from proxy.config import get_settings

SESSION = "0f5c2b9e-1234-4abc-9def-1234567890ab"
USER_ID = "user_" + "a" * 64 + "_account_11111111-2222-3333-4444-555555555555_session_" + SESSION


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def streamed(status, data, content_type="application/json"):
    """A response that streams like a real network response (not pre-read)."""
    if not isinstance(data, bytes):
        data = json.dumps(data).encode()
    return httpx.Response(status, headers={"content-type": content_type}, stream=_Stream(data))


class FakeUpstream:
    """Records what the proxy forwards to 'api.anthropic.com' and answers like it."""

    def __init__(self, reject_models=(), fail=False):
        self.requests = []
        self.reject_models = set(reject_models)
        self.fail = fail

    def __call__(self, request):
        if self.fail:
            raise httpx.ConnectError("no internet", request=request)
        body = json.loads(request.content) if request.content else None
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body,
                              "raw": request.content})
        if body and body.get("model") in self.reject_models:
            return streamed(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                             "message": "unsupported for this model"}})
        if body and body.get("stream"):
            sse = b"event: message_start\ndata: {\"type\":\"message_start\"}\n\nevent: message_stop\ndata: {}\n\n"
            return streamed(200, sse, "text/event-stream")
        return streamed(200, {"id": "msg_1", "model": body.get("model") if body else None})


def make_client(tmp_path, upstream=None, **env):
    base = {"MODELMATCH_LOG_DIR": str(tmp_path / "logs"),
            "MODELMATCH_STATE_FILE": str(tmp_path / "state" / "sessions.json")}
    base.update(env)
    settings = get_settings(env=base, root=tmp_path)
    upstream = upstream or FakeUpstream()
    app = create_app(settings, upstream_transport=httpx.MockTransport(upstream))
    return TestClient(app), upstream


def accept_recommendation(client, prompt="What is the capital of France?", accepted=True):
    client.post("/session/start", json={"session_id": SESSION, "source": "startup", "model": "claude-opus-5-5"})
    rec = client.post("/recommend", json={"session_id": SESSION, "prompt": prompt}).json()
    sel = client.post("/selection", json={"session_id": SESSION, "recommendation_id": rec["recommendation_id"],
                                           "accepted": accepted, "decided_by": "test"})
    return rec, sel


def messages_request(model="claude-opus-5-5", stream=False, user_id=USER_ID):
    return {"model": model, "max_tokens": 100, "stream": stream, "metadata": {"user_id": user_id},
            "messages": [{"role": "user", "content": "hi"}]}


# -- ModelMatch endpoints -------------------------------------------------


def test_health(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        data = client.get("/health").json()
    assert data["status"] == "ok"
    assert data["service"] == "modelmatch-proxy"
    assert data["router"] == "mock"


def test_recommend_and_accept_sets_route(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        rec, sel = accept_recommendation(client)
        assert rec["ok"] and rec["recommended_model"] == "Claude Haiku 4.5" and rec["routable"] is True
        assert rec["current_model"] == "claude-opus-5-5"
        assert sel.status_code == 200 and sel.json()["applied"] is True
        session = client.get("/session/" + SESSION).json()["session"]
    assert session["active_route"] == "claude-haiku-4-5"
    assert session["turn"]["accepted"] is True
    assert "prompt" not in json.dumps(session["turn"]).replace("prompt_id", "").replace("prompt_fingerprint", "")
    # state survives a proxy restart
    stored = json.loads((tmp_path / "state" / "sessions.json").read_text())
    assert stored["sessions"][SESSION]["active_route"] == "claude-haiku-4-5"


def test_reject_and_new_turn_clear_route(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        accept_recommendation(client, accepted=False)
        assert client.get("/session/" + SESSION).json()["session"]["active_route"] is None
        accept_recommendation(client, accepted=True)
        # next human prompt: the previous turn's route must not leak into it
        client.post("/recommend", json={"session_id": SESSION, "prompt": "another prompt"})
        assert client.get("/session/" + SESSION).json()["session"]["active_route"] is None


def test_unsupported_recommendation_is_never_applied(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        rec, sel = accept_recommendation(client, prompt="wc-test: GPT-4o")
    assert rec["provider"] == "openai" and rec["routable"] is False
    assert "isn't set up" in rec["routing_note"]
    assert sel.json()["applied"] is False


def test_stale_selection_is_rejected(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        accept_recommendation(client)
        resp = client.post("/selection", json={"session_id": SESSION, "recommendation_id": "nope", "accepted": True})
    assert resp.status_code == 409


def test_router_failure_returns_502_for_fail_open(tmp_path):
    client, _ = make_client(tmp_path, MODELMATCH_API_URL="http://127.0.0.1:9/api/route",
                            MODELMATCH_API_TIMEOUT="2")
    with client:
        resp = client.post("/recommend", json={"session_id": SESSION, "prompt": "hi"})
    assert resp.status_code == 502
    assert resp.json()["ok"] is False


def test_mock_cloud_contract_endpoint(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        data = client.post("/mock/api/route", json={"prompt": "hi there", "client": "claude-code"}).json()
    assert set(data) == {"recommended_model", "reason", "confidence"}


# -- passthrough -----------------------------------------------------------------


def test_passthrough_untouched_without_route(tmp_path):
    client, upstream = make_client(tmp_path)
    raw = json.dumps(messages_request()).encode()
    with client:
        resp = client.post("/v1/messages?beta=true", content=raw,
                           headers={"x-api-key": "sk-test", "anthropic-version": "2023-06-01",
                                    "content-type": "application/json"})
    assert resp.status_code == 200
    sent = upstream.requests[0]
    assert sent["url"] == "https://api.anthropic.com/v1/messages?beta=true"
    assert sent["raw"] == raw  # byte-identical
    assert sent["headers"]["x-api-key"] == "sk-test"
    assert sent["headers"]["host"] == "api.anthropic.com"


def test_passthrough_routes_accepted_model_and_streams(tmp_path):
    client, upstream = make_client(tmp_path)
    with client:
        accept_recommendation(client)
        resp = client.post("/v1/messages", json=messages_request(stream=True),
                           headers={"authorization": "Bearer oauth-token"})
    assert resp.status_code == 200
    assert resp.headers["x-modelmatch-routed-model"] == "claude-haiku-4-5"
    assert b"message_start" in resp.content
    sent = upstream.requests[0]
    assert sent["body"]["model"] == "claude-haiku-4-5"
    assert sent["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["headers"]["authorization"] == "Bearer oauth-token"


def test_background_haiku_requests_and_other_paths_are_not_routed(tmp_path):
    client, upstream = make_client(tmp_path)
    with client:
        accept_recommendation(client, prompt="Fix this bug in my python code")  # -> Sonnet
        client.post("/v1/messages", json=messages_request(model="claude-haiku-4-5-20251001"))
        client.post("/v1/messages/count_tokens", json=messages_request())
    assert upstream.requests[0]["body"]["model"] == "claude-haiku-4-5-20251001"
    assert upstream.requests[1]["body"]["model"] == "claude-opus-5-5"


def test_other_sessions_are_not_routed(tmp_path):
    client, upstream = make_client(tmp_path)
    other = USER_ID.replace(SESSION, "99999999-9999-4999-8999-999999999999")
    with client:
        accept_recommendation(client)
        client.post("/v1/messages", json=messages_request(user_id=other))
        client.post("/v1/messages", json=messages_request(user_id="no-session-here"))
    assert [r["body"]["model"] for r in upstream.requests] == ["claude-opus-5-5", "claude-opus-5-5"]


def test_rejected_routed_request_falls_back_to_original_model(tmp_path):
    client, upstream = make_client(tmp_path, upstream=FakeUpstream(reject_models={"claude-haiku-4-5"}))
    with client:
        accept_recommendation(client)
        first = client.post("/v1/messages", json=messages_request())
        second = client.post("/v1/messages", json=messages_request())
        session = client.get("/session/" + SESSION).json()["session"]
    assert first.status_code == 200 and second.status_code == 200
    assert "x-modelmatch-routed-model" not in first.headers
    assert [r["body"]["model"] for r in upstream.requests] == ["claude-haiku-4-5", "claude-opus-5-5",
                                                                 "claude-opus-5-5"]
    assert session["route_status"] == "failed"


def test_upstream_unreachable_returns_anthropic_style_error(tmp_path):
    client, _ = make_client(tmp_path, upstream=FakeUpstream(fail=True))
    with client:
        resp = client.post("/v1/messages", json=messages_request())
    assert resp.status_code == 502
    assert resp.json()["type"] == "error"


def test_logs_never_contain_secrets_or_prompts(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        accept_recommendation(client, prompt="my secret project codename is BLUEBIRD")
        client.post("/v1/messages", json=messages_request(), headers={"x-api-key": "sk-ant-SECRET"})
    text = (tmp_path / "logs" / "proxy.log").read_text()
    assert "BLUEBIRD" not in text
    assert "sk-ant-SECRET" not in text
    assert "routing | session=0f5c2b9e | claude-opus-5-5 -> claude-haiku-4-5" in text


@pytest.mark.parametrize("headers, body, expected", [
    ({"x-claude-code-session-id": "abc"}, {}, ("abc", "header")),
    ({}, {"metadata": {"user_id": USER_ID}}, (SESSION, "metadata")),
    ({}, {"metadata": {"user_id": json.dumps({"device_id": "d", "session_id": "s-1"})}}, ("s-1", "metadata-json")),
    ({}, {"metadata": {"user_id": "plain"}}, (None, None)),
    ({}, {}, (None, None)),
])
def test_extract_session_id(headers, body, expected):
    assert extract_session_id(headers, body) == expected
