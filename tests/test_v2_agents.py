"""V2 for Codex CLI and GitHub Copilot CLI (cross-client parity)."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.agents.app import create_app
from proxy.agents.config import get_agent_settings
from tests.test_agents_app import CATALOG, SESSION, codex_headers, responses_request
from tests.test_v2_app import FakeCurvet, analysis

ROOT = Path(__file__).resolve().parent.parent
TURN1, TURN2, TURN3 = "turn-0001", "turn-0002", "turn-0003"
COPILOT_MODELS = [{"id": "claude-opus-4.7", "name": "Claude Opus 4.7", "category": "powerful"},
                  {"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6", "category": "versatile"},
                  {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini", "category": "lightweight"}]


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def completed(model, total=50000, cached=48000, output=300):
    event = {"type": "response.completed", "response": {"model": model, "status": "completed", "usage": {
        "input_tokens": total, "input_tokens_details": {"cached_tokens": cached}, "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": 100}}}}
    return ("event: response.created\ndata: {\"type\":\"response.created\"}\n\nevent: response.completed\ndata: "
            + json.dumps(event) + "\n\n").encode()


class FakeCodexApi:
    def __init__(self):
        self.models = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.models.append(body["model"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream(completed(body["model"])))


@pytest.fixture
def codex_home(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    (home / "models_cache.json").write_text(json.dumps(CATALOG))
    return home


def make(tmp_path, codex_home, **env):
    base = {"MODELMATCH_LOG_DIR": str(tmp_path / "logs"), "MODELMATCH_AGENTS_STATE_FILE": str(tmp_path / "a.json"),
            "MODELMATCH_AGENTS_DB_FILE": str(tmp_path / "agents.db"), "MODELMATCH_CODEX_HOME": str(codex_home),
            "MODELMATCH_ROUTER_V2": "1", "MODELMATCH_API_URL": "https://curvet.in/api/v1/chat/completions",
            "MODELMATCH_API_KEY": "app_x", "MODELMATCH_API_SECRET": "s" * 64}
    base.update(env)
    upstream, curvet = FakeCodexApi(), FakeCurvet()
    app = create_app(get_agent_settings(env=base, root=tmp_path), upstream_transport=httpx.MockTransport(upstream),
                     router_transport=httpx.MockTransport(curvet))
    return TestClient(app), upstream


def recommend(client, tool, prompt, turn=None, current=None, models=None, routing=True):
    payload = {"client": tool, "session_id": SESSION, "prompt": prompt, "turn_id": turn, "current_model": current,
               "routing": routing, "client_version": 2}
    if models is not None:
        payload["available_models"] = models
    return client.post("/recommend", json=payload).json()


def select(client, tool, rec, choice):
    return client.post("/selection", json={"client": tool, "session_id": SESSION, "decided_by": "dialog",
                                           "recommendation_id": rec["recommendation_id"], "accepted": True,
                                           "choice": choice}).json()


def codex_call(client, turn, model="gpt-5.6-sol"):
    body = responses_request(model)
    body["client_metadata"]["turn_id"] = turn
    return client.post("/v1/responses", json=body, headers=codex_headers(turn=turn))


def test_codex_v2_recommends_from_its_own_list_and_leases_across_turns(tmp_path, codex_home):
    client, upstream = make(tmp_path, codex_home)
    with client:
        client.post("/session/start", json={"client": "codex", "session_id": SESSION, "source": "startup",
                                            "model": "gpt-5.6-sol"})
        rec = recommend(client, "codex", "Now implement the React login form.", TURN1, "gpt-5.6-sol")
        v2 = rec["v2"]
        assert v2["intervention"] == "ASK" and v2["candidate_model"] == "gpt-5.6-terra"
        assert [o["label"] for o in v2["options"]] == ["Use GPT-5.6-Terra", "Keep GPT-5.6-Sol"]
        assert select(client, "codex", rec, "gpt-5.6-terra")["applied"]
        codex_call(client, TURN1)
        follow = recommend(client, "codex", "Show the validation message too.", TURN2, "gpt-5.6-sol")
        assert follow["v2"]["intervention"] == "SILENT" and follow["v2"]["final_model"] == "gpt-5.6-terra"
        codex_call(client, TURN2)  # a new turn: the task's model still applies
        assert upstream.models == ["gpt-5.6-terra", "gpt-5.6-terra"]
        metrics = client.get("/v2/metrics?client=codex").json()
    usage = metrics["measured_usage_by_model"]["gpt-5.6-terra"]
    assert usage["requests"] == 2 and usage["cache_read_tokens"] == 96000 and usage["input_tokens"] == 4000


def test_codex_cached_tokens_inform_the_stay_or_switch_check(tmp_path, codex_home):
    client, upstream = make(tmp_path, codex_home)
    with client:
        recommend(client, "codex", "Design authentication architecture.", TURN1, "gpt-5.6-sol")
        codex_call(client, TURN1)  # sol now holds ~50k tokens, 48k of them cached
        rec = recommend(client, "codex", "Make the button text bold.", TURN2, "gpt-5.6-sol")
    v2 = rec["v2"]
    assert v2["cache"]["context_tokens"] == 50000 and v2["cache"]["current"] == "WARM"
    assert v2["economics"]["stay"]["type"] == "TOKENS"  # no Codex price list: token counts, never dollars
    assert v2["final_model"] == "gpt-5.6-sol" and "SWITCH_USAGE_PENALTY" in v2["reason_codes"]


def test_copilot_v2_uses_the_extension_model_list_and_says_cache_is_unknown(tmp_path, codex_home):
    client, _ = make(tmp_path, codex_home)
    with client:
        rec = recommend(client, "copilot", "Find the race condition in checkout.", current="claude-sonnet-4.6",
                        models=COPILOT_MODELS)
        v2 = rec["v2"]
        assert v2["intervention"] == "ASK" and v2["candidate_model"] == "claude-opus-4.7"
        assert v2["cache"]["current"] == "UNKNOWN" and v2["economics"]["verdict"] == "unknown"
        picked = select(client, "copilot", rec, "claude-opus-4.7")
        assert picked["message"] == "ModelMatch: using Claude Opus 4.7 for this task."
        follow = recommend(client, "copilot", "Still broken, the duplicate payment still happens.",
                           current="claude-sonnet-4.6", models=COPILOT_MODELS)
    assert follow["v2"]["intervention"] == "SILENT" and follow["v2"]["final_model"] == "claude-opus-4.7"


def test_copilot_routing_off_only_suggests(tmp_path, codex_home):
    client, _ = make(tmp_path, codex_home)
    with client:
        rec = recommend(client, "copilot", "Find the race condition in checkout.", current="claude-sonnet-4.6",
                        models=COPILOT_MODELS, routing=False)
    assert rec["v2"]["intervention"] == "SILENT" and "Routing is off" in rec["v2"]["note"]


def load(name):
    hooks = str(ROOT / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    spec = importlib.util.spec_from_file_location(name + "_v2_test", str(ROOT / "hooks" / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("outcome, current, expected", [
    ({"model_id": "claude-opus-4.7", "model_name": "Claude Opus 4.7", "message": "ModelMatch: using it."},
     "claude-sonnet-4.6", {"action": "switch", "model_id": "claude-opus-4.7"}),
    ({"model_id": "claude-opus-4.7", "model_name": None, "message": None}, "claude-sonnet-4.6",
     {"action": "switch", "message": "ModelMatch: using claude-opus-4.7 for this task."}),
    ({"model_id": "claude-sonnet-4.6", "model_name": None, "message": None}, "claude-sonnet-4.6",
     {"action": "keep", "message": None}),
    ({"model_id": None, "model_name": None, "message": "ModelMatch couldn't analyse this prompt."},
     "claude-sonnet-4.6", {"action": "keep"}),
])
def test_copilot_hook_turns_v2_decisions_into_extension_actions(monkeypatch, outcome, current, expected):
    copilot = load("modelmatch_copilot_hook")
    monkeypatch.setattr(copilot.common, "run_v2_decision", lambda cfg, log, client, sid, rec: outcome)
    answer = copilot.v2_answer(None, None, SESSION, {"v2": {}}, current)
    for key, value in expected.items():
        assert answer[key] == value


def test_codex_hook_passes_v2_decisions_through(monkeypatch):
    common = load("agents_common")
    calls = []

    def fake(cfg, session_id, rec, post=None, extra=None, logger=None):
        calls.append(extra)
        return {"message": "ModelMatch: Still on GPT-5.6-Terra for this task.", "model_id": None, "model_name": None}

    monkeypatch.setattr(common.base, "run_v2_decision", fake)
    assert common.run_v2_decision(None, None, "codex", SESSION, {"v2": {}})["message"].startswith("ModelMatch")
    assert calls == [{"client": "codex"}]
