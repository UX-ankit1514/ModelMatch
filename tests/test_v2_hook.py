"""The Claude Code hook's V2 behaviour: silent, note, or a two-choice question."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_hook import VENV_PYTHON, base_env, free_port, get_json, prompt_event, run_hook

ROOT = Path(__file__).resolve().parent.parent
SESSION = "5e55a0b1-0000-4000-8000-00000000abcd"


def load_hook():
    spec = importlib.util.spec_from_file_location("modelmatch_hook_v2_test", str(ROOT / "hooks" / "modelmatch_hook.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load_hook()


def config(**env):
    values = {"MODELMATCH_CONFIRM_UI": "dialog", "MODELMATCH_CONFIRM_TIMEOUT": "3"}
    values.update(env)
    return hook.Config(env=values)


def decision(intervention="ASK", stay_first=False, note=None):
    keep = {"id": "keep", "label": "Stay on Opus 5.5" if stay_first else "Keep Opus 5.5", "model_id": None,
            "primary": stay_first}
    use = {"id": "claude-sonnet-5-5", "label": "Use Sonnet 5.5 anyway" if stay_first else "Use Sonnet 5.5",
           "model_id": "claude-sonnet-5-5", "primary": not stay_first}
    options = [keep, use] if stay_first else [use, keep]
    return {"recommendation_id": "rec1", "v2": {
        "intervention": intervention, "options": options if intervention == "ASK" else [], "note": note,
        "headline": "Recommended: Stay on Opus 5.5" if stay_first else "Recommended: Sonnet 5.5",
        "explanation": "Because.", "cost_note": None, "running_model": "claude-opus-5-5",
        "final_model": "claude-opus-5-5", "candidate_model": "claude-sonnet-5-5", "reason_codes": []}}


class Recorder:
    def __init__(self, message="ModelMatch: using Sonnet 5.5 for this task."):
        self.calls = []
        self.message = message

    def __call__(self, method, url, payload=None, timeout=5.0):
        self.calls.append(payload)
        return 200, {"ok": True, "applied": payload["choice"] != "keep", "message": self.message}


@pytest.fixture
def popup(tmp_path, monkeypatch):
    """A fake osascript on PATH: FAKE_POPUP=primary|other|keep|timeout."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "osascript"
    script.write_text("""#!/bin/bash
while [ "$1" != "--" ]; do shift; done; shift
echo "$@" > "$(dirname "$0")/args.txt"
case "$FAKE_POPUP" in
  primary) echo "BUTTON:$2" ;;
  other)   echo "BUTTON:$3" ;;
  keep)    echo "execution error: User canceled. (-128)" >&2; exit 1 ;;
  timeout) echo "TIMEOUT" ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(hook.sys, "platform", "darwin")
    return bin_dir


def test_silent_decision_says_nothing():
    recorder = Recorder()
    assert hook.handle_v2_decision(config(), SESSION, decision("SILENT"), post=recorder) is None
    assert recorder.calls == []


def test_auto_and_lease_decisions_show_their_note_without_asking():
    recorder = Recorder()
    note = "ModelMatch: Still on Sonnet 5.5 for this task."
    assert hook.handle_v2_decision(config(), SESSION, decision("SILENT", note=note), post=recorder) == note
    auto = "ModelMatch: Using Opus 5.5 as you asked."
    assert hook.handle_v2_decision(config(), SESSION, decision("AUTO", note=auto), post=recorder) == auto
    assert recorder.calls == []


@pytest.mark.parametrize("answer, choice, accepted", [("primary", "claude-sonnet-5-5", True),
                                                      ("other", "keep", False), ("keep", "keep", False)])
def test_ask_switch_recommended(popup, monkeypatch, answer, choice, accepted):
    monkeypatch.setenv("FAKE_POPUP", answer)
    recorder = Recorder()
    message = hook.handle_v2_decision(config(), SESSION, decision("ASK"), post=recorder)
    sent = recorder.calls[0]
    assert sent["choice"] == choice and sent["accepted"] is accepted and sent["decided_by"] == "dialog"
    assert sent["recommendation_id"] == "rec1" and message == recorder.message
    args = (popup / "args.txt").read_text()
    assert "Recommended: Sonnet 5.5" in args and "Use Sonnet 5.5" in args and "Keep Opus 5.5" in args


@pytest.mark.parametrize("answer, choice", [("primary", "keep"), ("other", "claude-sonnet-5-5"), ("keep", "keep")])
def test_ask_stay_recommended(popup, monkeypatch, answer, choice):
    monkeypatch.setenv("FAKE_POPUP", answer)
    recorder = Recorder("ModelMatch: staying on Opus 5.5.")
    hook.handle_v2_decision(config(), SESSION, decision("ASK", stay_first=True), post=recorder)
    assert recorder.calls[0]["choice"] == choice


def test_timeout_and_unavailable_keep_the_model(popup, monkeypatch):
    monkeypatch.setenv("FAKE_POPUP", "timeout")
    recorder = Recorder()
    message = hook.handle_v2_decision(config(), SESSION, decision("ASK"), post=recorder)
    assert recorder.calls[0]["choice"] == "keep" and recorder.calls[0]["decided_by"] == "timeout"
    assert "no answer within 3s" in message
    monkeypatch.setattr(hook.sys, "platform", "linux")  # no popup available
    recorder = Recorder("ModelMatch: no answer, so Opus 5.5 was kept.")
    hook.handle_v2_decision(config(), SESSION, decision("ASK"), post=recorder)
    assert recorder.calls[0]["choice"] == "keep" and recorder.calls[0]["decided_by"] == "unavailable"


def test_confirm_ui_modes():
    recorder = Recorder()
    hook.handle_v2_decision(config(MODELMATCH_CONFIRM_UI="auto-accept"), SESSION, decision("ASK"), post=recorder)
    hook.handle_v2_decision(config(MODELMATCH_CONFIRM_UI="never"), SESSION, decision("ASK"), post=recorder)
    assert [c["choice"] for c in recorder.calls] == ["claude-sonnet-5-5", "keep"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS popup")
def test_hook_and_v2_proxy_end_to_end(tmp_path, popup):
    """A real proxy process with V2 on (offline analyst) and the real hook script."""
    port = free_port()
    env = base_env(tmp_path, port, MODELMATCH_ROUTER_V2="1", MODELMATCH_DB_FILE=str(tmp_path / "mm.db"),
                   MODELMATCH_CLAUDE_POLICY_FILES="")
    process = subprocess.Popen([str(VENV_PYTHON), "-m", "uvicorn", "proxy.app:create_app", "--factory", "--host",
                                "127.0.0.1", "--port", str(port), "--log-level", "warning"], cwd=str(ROOT), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = "http://127.0.0.1:{}".format(port)
    try:
        for _ in range(50):
            try:
                assert get_json(url + "/health")["v2"] is True
                break
            except OSError:
                time.sleep(0.1)
        request = urllib.request.Request(url + "/session/start", method="POST", headers={
            "Content-Type": "application/json"}, data=json.dumps({"session_id": SESSION, "source": "startup",
                                                                  "model": "claude-opus-5-5"}).encode())
        urllib.request.urlopen(request, timeout=3).read()
        hook_env = dict(env, MODELMATCH_CONFIRM_UI="dialog", FAKE_POPUP="primary",
                        ANTHROPIC_BASE_URL=url, PATH=str(popup) + os.pathsep + env.get("PATH", ""))
        result = run_hook(prompt_event("What is the capital of France?"), hook_env, start_new_session=True)
        assert result.message == "ModelMatch: using Haiku 4.5 for this task."
        session = get_json(url + "/session/" + SESSION)["session"]
        assert session["active_route"] == "claude-haiku-4-5"
    finally:
        process.terminate()
        process.wait(timeout=5)
