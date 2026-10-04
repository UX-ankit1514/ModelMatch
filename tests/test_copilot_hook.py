"""End-to-end tests of hooks/modelmatch_copilot_hook.py (the extension's decision-maker and the
--hooks-only command hook) against a real agents proxy process."""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "modelmatch_copilot_hook.py"
EXTENSION = ROOT / "hooks" / "copilot-extension" / "extension.mjs"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
SESSION = "f2a2a46d-817b-4137-beeb-7dd08df20c4f"
OFFERED = [{"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6", "category": "versatile"},
           {"id": "claude-haiku-4.5", "name": "Claude Haiku 4.5", "category": "lightweight"},
           {"id": "claude-opus-4.8", "name": "Claude Opus 4.8", "category": "powerful"}]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def base_env(tmp_path, port, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MODELMATCH_", "COPILOT", "CLAUDE"))}
    env.update({
        "MODELMATCH_AGENTS_PORT": str(port),
        "MODELMATCH_LOG_DIR": str(tmp_path / "logs"),
        "MODELMATCH_STATE_DIR": str(tmp_path / "state"),
        "MODELMATCH_AGENTS_STATE_FILE": str(tmp_path / "state" / "agents.json"),
        "MODELMATCH_CODEX_HOME": str(tmp_path / "codex"),
        "MODELMATCH_API_URL": "",
        "MODELMATCH_CONFIRM_UI": "never",
        "MODELMATCH_CONFIRM_TIMEOUT": "3",
        "MODELMATCH_DISABLED": "0",
        "MODELMATCH_NO_LAUNCHD": "1",
    })
    env.update({k: str(v) for k, v in extra.items()})
    return env


def get_json(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


@pytest.fixture
def proxy(tmp_path):
    port = free_port()
    env = base_env(tmp_path, port)
    process = subprocess.Popen([str(VENV_PYTHON), "-m", "uvicorn", "proxy.agents.app:create_app", "--factory",
                                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning", "--ws", "none"],
                               cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = "http://127.0.0.1:{}".format(port)
    for _ in range(50):
        try:
            get_json(url + "/health")
            break
        except OSError:
            time.sleep(0.1)
    yield {"port": port, "url": url, "tmp": tmp_path}
    process.terminate()
    process.wait(timeout=5)


def decide(payload, env, **popen):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run([sys.executable, str(HOOK), "--extension"], input=data, capture_output=True, text=True,
                            env=env, timeout=30, **popen)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def prompt(text="What is the capital of France?", current="claude-sonnet-4.6", **extra):
    payload = {"event": "prompt", "session_id": SESSION, "cwd": str(ROOT), "prompt": text, "current_model": current,
               "available_models": OFFERED, "interactive": True, "can_switch": True}
    payload.update(extra)
    return payload


def session_state(proxy):
    return get_json(proxy["url"] + "/session/copilot/" + SESSION)["session"]


# -- extension mode ---------------------------------------------------------------------


def test_session_start_message(proxy):
    env = base_env(proxy["tmp"], proxy["port"])
    answer = decide({"event": "session_start", "session_id": SESSION, "source": "startup", "model": "claude-sonnet-4.6",
                     "interactive": True, "can_switch": True}, env)
    assert answer["message"].startswith("ModelMatch is on")
    assert session_state(proxy)["start_model"] == "claude-sonnet-4.6"
    assert decide({"event": "session_start", "session_id": SESSION, "source": "resume"}, env) == {}
    old = decide({"event": "session_start", "session_id": SESSION, "source": "new", "can_switch": False}, env)
    assert "update Copilot CLI to 1.0.44" in old["message"]


def test_accept_answers_switch(proxy):
    answer = decide(prompt(), base_env(proxy["tmp"], proxy["port"], MODELMATCH_CONFIRM_UI="auto-accept"))
    assert answer["action"] == "switch"
    assert answer["model_id"] == "claude-haiku-4.5" and answer["model_name"] == "Claude Haiku 4.5"
    assert answer["message"].startswith("ModelMatch: running this prompt on Claude Haiku 4.5.")
    assert session_state(proxy)["turn"]["accepted"] is True


def test_reject_answers_keep(proxy):
    answer = decide(prompt(), base_env(proxy["tmp"], proxy["port"], MODELMATCH_CONFIRM_UI="never"))
    assert answer == {"action": "keep", "message": "ModelMatch: kept your current model (recommended Claude "
                                                   "Haiku 4.5)."}


def test_same_model_unavailable_and_old_copilot(proxy):
    env = base_env(proxy["tmp"], proxy["port"], MODELMATCH_CONFIRM_UI="auto-accept")
    same = decide(prompt(current="claude-haiku-4.5"), env)
    assert same["action"] == "keep" and "already your current model" in same["message"]
    missing = decide(prompt("wc-test: Grok 9"), env)
    assert missing["action"] == "keep" and "Grok 9 isn't in your Copilot plan." in missing["message"]
    no_list = decide(prompt(available_models=[]), env)
    assert no_list["action"] == "keep" and "Couldn't read your Copilot plan" in no_list["message"]
    old = decide(prompt(can_switch=False), env)
    assert old["action"] == "keep" and "update Copilot CLI to 1.0.44" in old["message"]


def test_family_substitution_is_explained(proxy):
    offered = [{"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6"}, {"id": "claude-haiku-4.5", "name": "Claude Haiku 4.5"}]
    env = base_env(proxy["tmp"], proxy["port"], MODELMATCH_CONFIRM_UI="auto-accept")
    # (Not one of the built-in recommender's own three answers, which stand for sizes, not exact models.)
    answer = decide(prompt("wc-test: Claude Sonnet 4.9", current="claude-haiku-4.5", available_models=offered), env)
    assert answer["model_id"] == "claude-sonnet-4.6"
    assert "named Claude Sonnet 4.9; this is the closest model in your Copilot plan" in answer["message"]


def test_scripted_runs_slash_commands_and_pause_keep_quietly(proxy):
    env = base_env(proxy["tmp"], proxy["port"], MODELMATCH_CONFIRM_UI="auto")
    assert decide(prompt(interactive=False), env) == {"action": "keep", "message": None}
    assert decide(prompt("/model"), env) == {"action": "keep", "message": None}
    paused = base_env(proxy["tmp"], proxy["port"], MODELMATCH_COPILOT_DISABLED="1")
    assert decide(prompt(), paused) == {"action": "keep", "message": None}
    auto = base_env(proxy["tmp"], proxy["port"], MODELMATCH_CONFIRM_UI="auto-accept")
    assert decide(prompt(interactive=False), auto)["action"] == "switch"  # explicit opt-in for automation


@pytest.mark.parametrize("payload", ["", "not json", "[1]", json.dumps({"event": "mystery"}),
                                     json.dumps({"event": "prompt"})])
def test_malformed_input_means_keep(proxy, payload):
    answer = decide(payload, base_env(proxy["tmp"], proxy["port"]))
    assert answer.get("action", "keep") == "keep"


def test_proxy_down_means_keep_quickly(tmp_path):
    env = base_env(tmp_path, free_port(), MODELMATCH_PYTHON=str(tmp_path / "missing-python"))
    started = time.monotonic()
    answer = decide(prompt(), env)
    assert answer["action"] == "keep" and "unavailable right now" in answer["message"]
    assert time.monotonic() - started < 5


def test_switch_results_are_logged(proxy):
    env = base_env(proxy["tmp"], proxy["port"])
    assert decide({"event": "switch_result", "session_id": SESSION, "ok": False, "detail": "model gone"}, env) == {}
    assert "Copilot did not switch: model gone" in (proxy["tmp"] / "logs" / "copilot-hook.log").read_text()


def test_logs_never_contain_prompts(proxy):
    decide(prompt("Secret codename BLUEBIRD"), base_env(proxy["tmp"], proxy["port"]))
    for name in ("copilot-hook.log", "agents-proxy.log"):
        assert "BLUEBIRD" not in (proxy["tmp"] / "logs" / name).read_text()


# -- --hooks-only command hooks -----------------------------------------------------------


@pytest.fixture
def fake_osascript(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "osascript"
    script.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$FAKE_NOTIFY_LOG"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_command_hook_shows_a_notification(proxy, fake_osascript):
    env = base_env(proxy["tmp"], proxy["port"], FAKE_NOTIFY_LOG=str(proxy["tmp"] / "notify.txt"))
    env["PATH"] = str(fake_osascript) + os.pathsep + env["PATH"]
    payload = {"sessionId": SESSION, "timestamp": 1791040426681, "cwd": str(ROOT), "prompt": "what is 2+2?"}
    result = subprocess.run([sys.executable, str(HOOK), "--event", "userPromptSubmitted"], input=json.dumps(payload),
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0 and result.stdout == ""
    shown = (proxy["tmp"] / "notify.txt").read_text()
    assert "Recommended: Claude Haiku 4.5" in shown and "Switch with /model" in shown
    assert session_state(proxy)["turn"]["decided_by"] == "hooks-only"


def test_command_hook_session_start_and_bad_input(proxy):
    env = base_env(proxy["tmp"], proxy["port"])
    for event, data in (("sessionStart", {"sessionId": SESSION, "source": "new", "cwd": "/tmp"}),
                        ("userPromptSubmitted", "garbage"), ("somethingElse", {})):
        result = subprocess.run([sys.executable, str(HOOK), "--event", event],
                                input=data if isinstance(data, str) else json.dumps(data), capture_output=True,
                                text=True, env=env, timeout=30)
        assert result.returncode == 0 and result.stdout == ""
    assert session_state(proxy)["source"] == "new"


def test_prompt_mode_detection():
    sys.path.insert(0, str(ROOT / "hooks"))
    import modelmatch_copilot_hook as hook
    assert hook.copilot_prompt_mode("/opt/homebrew/bin/copilot -p say hello --allow-all-tools")
    assert hook.copilot_prompt_mode("copilot --prompt=hi")
    assert not hook.copilot_prompt_mode("/opt/homebrew/bin/copilot")
    assert not hook.copilot_prompt_mode("/bin/bash -c python3 hook.py -p")


# -- the extension file itself -------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_extension_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(EXTENSION)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_extension_never_writes_to_stdout():
    source = EXTENSION.read_text()
    assert "console.log" not in source and "process.stdout" not in source  # stdout is Copilot's JSON-RPC channel
    assert '"modelmatch_copilot_hook.py"' in source and "--extension" in source
