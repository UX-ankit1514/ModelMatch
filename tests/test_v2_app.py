"""V2 end to end through the Claude Code proxy: fake Curvet analyst, fake Anthropic API."""

import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.config import get_settings

SESSION = "0f5c2b9e-1234-4abc-9def-1234567890ab"
USER_ID = "user_" + "a" * 64 + "_account_11111111-2222-3333-4444-555555555555_session_" + SESSION
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"


@pytest.fixture(autouse=True)
def no_policy_files(monkeypatch):
    monkeypatch.setenv("MODELMATCH_CLAUDE_POLICY_FILES", "")  # never read the machine's real Claude settings


def analysis(task_type, tier, complexity="medium", **extra):
    data = {"task_type": task_type, "subtype": task_type.replace("_", " "), "complexity": complexity,
            "reasoning_requirement": complexity, "scope": "multi_file", "risk": "low", "expected_output": "short",
            "expected_tool_steps": "few", "required_tier": tier, "confidence": 0.9, "followup_kind": "new_task",
            "same_task_probability": None, "explicit_model_request": None, "reason_codes": [],
            "reason": "ANALYST-SENTENCE echoing the prompt"}
    data.update(extra)
    return data


# What the fake analyst answers, by a word in the prompt (test fixtures, not routing rules).
SCRIPT = {
    "authentication architecture": analysis("architecture", "heavy", "high"),
    "token rotation": analysis("architecture", "heavy", "high", followup_kind="continuation",
                               same_task_probability=0.85),
    "login form": analysis("frontend_implementation", "standard", followup_kind="continuation",
                           same_task_probability=0.8),
    "validation message": analysis("frontend_implementation", "light", "low", followup_kind="continuation",
                                   same_task_probability=0.9),
    "button": analysis("frontend_ui", "standard", "low"),
    "race condition": analysis("complex_debugging", "heavy", "high"),
    "still broken": analysis("complex_debugging", "heavy", "high", followup_kind="retry_or_correction",
                             same_task_probability=0.9),
}


class FakeCurvet:
    def __init__(self, down=False):
        self.down = down
        self.requests = []

    def __call__(self, request):
        if self.down:
            raise httpx.ConnectError("down", request=request)
        body = json.loads(request.content)
        user = json.loads(body["messages"][1]["content"])
        self.requests.append(user)
        answer = next((a for word, a in SCRIPT.items() if word in user["request"].lower()),
                      analysis("unknown", "standard"))
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": json.dumps(answer)}}]})


def sse(model, ctx_cached=0, ctx_new=1200, output=150, stop="end_turn"):
    start = {"type": "message_start", "message": {"id": "m", "model": model, "usage": {
        "input_tokens": ctx_new, "cache_read_input_tokens": ctx_cached, "cache_creation_input_tokens": 800,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 800}, "output_tokens": 1}}}
    delta = {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": output}}
    return ("event: message_start\ndata: " + json.dumps(start) + "\n\nevent: content_block_delta\ndata: "
            + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "SECRET-ANSWER"}})
            + "\n\nevent: message_delta\ndata: " + json.dumps(delta) + "\n\nevent: message_stop\ndata: {}\n\n").encode()


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data[:200]  # in pieces, like the network
        yield self.data[200:]


class FakeAnthropic:
    def __init__(self, cached=0):
        self.requests = []
        self.cached = cached

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append({"model": body["model"], "headers": dict(request.headers)})
        return httpx.Response(200, headers={"content-type": "text/event-stream",
                                            "anthropic-ratelimit-unified-7d-utilization": "0.42"},
                              stream=_Stream(sse(body["model"], ctx_cached=self.cached)))


def make(tmp_path, curvet=None, upstream=None, **env):
    base = {"MODELMATCH_LOG_DIR": str(tmp_path / "logs"), "MODELMATCH_STATE_FILE": str(tmp_path / "sessions.json"),
            "MODELMATCH_DB_FILE": str(tmp_path / "mm.db"), "MODELMATCH_ROUTER_V2": "1",
            "MODELMATCH_API_URL": "https://curvet.in/api/v1/chat/completions", "MODELMATCH_API_KEY": "app_x",
            "MODELMATCH_API_SECRET": "s" * 64}
    base.update(env)
    curvet, upstream = curvet or FakeCurvet(), upstream or FakeAnthropic()
    app = create_app(get_settings(env=base, root=tmp_path), upstream_transport=httpx.MockTransport(upstream),
                     router_transport=httpx.MockTransport(curvet))
    return TestClient(app), curvet, upstream


def start(client, model=OPUS):
    client.post("/session/start", json={"session_id": SESSION, "source": "startup", "model": model})


def ask(client, prompt, routing=True):
    return client.post("/recommend", json={"session_id": SESSION, "prompt": prompt, "routing": routing,
                                           "client_version": 2}).json()


def call_model(client, model=OPUS):
    return client.post("/v1/messages", json={"model": model, "max_tokens": 100, "stream": True,
                                             "metadata": {"user_id": USER_ID},
                                             "system": [{"type": "text", "text": "x",
                                                         "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                                             "messages": [{"role": "user", "content": "hi"}]},
                       headers={"authorization": "Bearer sk-ant-oat01-SECRET-TOKEN"})


def choose(client, rec, choice=None, accepted=True, decided_by="dialog"):
    return client.post("/selection", json={"session_id": SESSION, "recommendation_id": rec["recommendation_id"],
                                           "accepted": accepted, "decided_by": decided_by, "choice": choice}).json()


def test_v2_off_is_exactly_v1(tmp_path):
    client, curvet, _ = make(tmp_path, MODELMATCH_ROUTER_V2="0")
    with client:
        assert client.get("/health").json()["v2"] is False
        rec = client.post("/recommend", json={"session_id": SESSION, "prompt": "fix the race condition"}).json()
        assert "v2" not in rec and rec["router_source"] == "curvet"
        assert client.get("/v2/metrics").status_code == 404
    assert not (tmp_path / "mm.db").exists()


def test_follow_up_keeps_the_task_and_a_new_phase_re_evaluates(tmp_path):
    client, curvet, upstream = make(tmp_path)
    with client:
        start(client)
        first = ask(client, "Design authentication architecture.")
        assert first["v2"]["intervention"] == "SILENT" and first["same_as_current"]
        assert first["v2"]["note"] is None  # nothing to say: Opus is right
        second = ask(client, "How should token rotation work?")
        assert second["v2"]["intervention"] == "SILENT" and second["v2"]["task"]["same_task"]
        assert "SAME_TASK_LEASE" in second["v2"]["reason_codes"]
        assert curvet.requests[1]["previous_task"]["task_type"] == "architecture"
        third = ask(client, "Now implement the React login form.")
        assert third["v2"]["task"]["same_task"] is False  # design -> implementation is a new phase
        assert third["v2"]["intervention"] == "ASK" and third["v2"]["candidate_model"] == SONNET
        assert [o["label"] for o in third["v2"]["options"]] == ["Use Sonnet 5.5", "Keep Opus 5.5"]


def test_accepted_switch_lasts_for_the_task_and_requests_are_routed(tmp_path):
    client, _, upstream = make(tmp_path)
    with client:
        start(client)
        rec = ask(client, "Now implement the React login form.")
        assert rec["v2"]["intervention"] == "ASK"
        picked = choose(client, rec, choice=SONNET)
        assert picked["applied"] and picked["message"] == "ModelMatch: using Sonnet 5.5 for this task."
        call_model(client)
        follow = ask(client, "Show the validation message under the field.")
        assert follow["v2"]["intervention"] == "SILENT" and follow["v2"]["final_model"] == SONNET
        assert follow["v2"]["note"] == "ModelMatch: Still on Sonnet 5.5 for this task."
        call_model(client)
        assert [r["model"] for r in upstream.requests] == [SONNET, SONNET]  # no bounce back to Opus
        decision = client.get("/v2/decision/" + rec["recommendation_id"]).json()["decision"]
    assert decision["user_choice"] == SONNET and decision["accepted"] == 1 and decision["ran_family"] == "sonnet"
    assert decision["outcome"] == "no_negative_signal" and decision["requests"] == 1


def test_trade_off_stay_on_warm_opus(tmp_path):
    """Large warm Opus context: a light frontend task does not switch silently to Sonnet."""
    client, _, upstream = make(tmp_path, upstream=FakeAnthropic(cached=95000))
    with client:
        start(client)
        ask(client, "Design authentication architecture.")
        call_model(client)  # Opus now holds ~97k tokens of warm context
        rec = ask(client, "Make the button text bold.")
        v2 = rec["v2"]
        assert v2["final_model"] == OPUS and v2["intervention"] == "SILENT"
        assert "SWITCH_USAGE_PENALTY" in v2["reason_codes"] and "CURRENT_CACHE_WARM" in v2["reason_codes"]
        assert v2["cache"]["context_tokens"] > 90000 and v2["cache"]["current"] == "WARM"
        assert v2["cache"]["billing"] == "subscription"
        assert v2["economics"]["stay"]["type"] == "SUBSCRIPTION_USAGE"
        call_model(client)
    assert upstream.requests[-1]["model"] == OPUS


def test_escalation_after_a_retry_is_recorded(tmp_path):
    client, _, _ = make(tmp_path)
    with client:
        start(client, model=SONNET)
        first = ask(client, "Find the race condition in checkout.")
        assert first["v2"]["intervention"] == "ASK" and first["v2"]["candidate_model"] == OPUS
        choose(client, first, choice="keep")  # the developer stays on Sonnet
        retry = ask(client, "Still broken, the duplicate payment still happens.")
        assert retry["v2"]["task"]["same_task"] and retry["v2"]["candidate_model"] == OPUS
        choose(client, retry, choice=OPUS)
        decision = client.get("/v2/decision/" + first["recommendation_id"]).json()["decision"]
        metrics = client.get("/v2/metrics").json()
    assert decision["ran_family"] == "sonnet" and decision["outcome"] == "escalated"
    assert decision["escalated_to"] == OPUS and decision["retry_detected"] == 1
    assert metrics["outcomes"]["escalation_rate"] == 1.0
    assert metrics["interventions"]["popup_rate"] == 1.0


def test_explicit_request_wins_and_is_applied(tmp_path):
    client, _, upstream = make(tmp_path)
    with client:
        start(client)
        rec = ask(client, "use haiku and rename this variable")
        assert rec["v2"]["intervention"] == "AUTO" and rec["v2"]["final_model"] == HAIKU
        assert "as you asked" in rec["v2"]["note"]
        call_model(client)
    assert upstream.requests[0]["model"] == HAIKU


def test_analyst_outage_fails_open_and_says_so_once(tmp_path):
    client, _, upstream = make(tmp_path, curvet=FakeCurvet(down=True))
    with client:
        start(client)
        first, second = ask(client, "Make the button text bold."), ask(client, "And the link.")
        call_model(client)
    for rec in (first, second):
        assert rec["ok"] and rec["v2"]["intervention"] == "SILENT" and rec["v2"]["final_model"] == OPUS
    assert "couldn't analyse" in first["v2"]["note"] and second["v2"]["note"] is None
    assert upstream.requests[0]["model"] == OPUS


def test_routing_off_only_suggests(tmp_path):
    client, _, upstream = make(tmp_path)
    with client:
        start(client)
        rec = ask(client, "Now implement the React login form.", routing=False)
    assert rec["v2"]["intervention"] == "SILENT" and "Routing is off" in rec["v2"]["note"]


def test_usage_is_measured_and_the_stream_is_unchanged(tmp_path):
    client, _, _ = make(tmp_path, upstream=FakeAnthropic(cached=5000))
    with client:
        start(client)
        ask(client, "Design authentication architecture.")
        response = call_model(client)
        metrics = client.get("/v2/metrics").json()
    assert response.content == sse(OPUS, ctx_cached=5000)
    usage = metrics["measured_usage_by_model"][OPUS]
    assert usage == {"requests": 1, "input_tokens": 1200, "output_tokens": 150, "cache_read_tokens": 5000,
                     "cache_write_tokens": 800, "refusals": 0, "errors": 0}
    db = sqlite3.connect(str(tmp_path / "mm.db"))
    event = db.execute("SELECT requested_ttl, billing_mode, rate_limits, stop_reason FROM usage_events").fetchone()
    assert event[0] == 3600 and event[1] == "subscription" and json.loads(event[2]) == {"7d-utilization": 0.42}
    assert event[3] == "end_turn"


def test_no_prompt_text_or_secrets_are_stored_or_logged(tmp_path):
    client, _, _ = make(tmp_path)
    with client:
        start(client)
        rec = ask(client, "Now implement the React login form for project BLUEBIRD with key sk-ant-ZZZZZZZZZZZZZZZZZZZZ")
        choose(client, rec, choice=SONNET)
        call_model(client)
    blob = (tmp_path / "mm.db").read_bytes() + (tmp_path / "logs" / "proxy.log").read_bytes()
    blob += (tmp_path / "sessions.json").read_bytes()
    for secret in (b"BLUEBIRD", b"sk-ant-ZZZZ", b"SECRET-TOKEN", b"SECRET-ANSWER", b"login form", b"s" * 64,
                   b"ANALYST-SENTENCE"):
        assert secret not in blob


def test_clear_or_compact_starts_a_new_task(tmp_path):
    client, _, _ = make(tmp_path)
    with client:
        start(client)
        rec = ask(client, "Now implement the React login form.")
        choose(client, rec, choice=SONNET)
        client.post("/session/start", json={"session_id": SESSION, "source": "clear"})
        after = ask(client, "Show the validation message under the field.")
    assert after["v2"]["task"]["same_task"] is False and after["v2"]["running_model"] == OPUS
