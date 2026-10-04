"""End-to-end tests of hooks/modelmatch_codex_hook.py against a real agents proxy process.

Every scenario checks the fail-open contract: exit code 0, and stdout is either
empty or a single JSON object with a "systemMessage".
"""

import itertools
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "modelmatch_codex_hook.py"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
SESSION = "01a10257-4d46-7ad2-9100-cbf0ebd364be"
CATALOG = {"models": [
    {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "description": "Older coding model for complex work."},
    {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "description": "Older balanced model."},
    {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "description": "Older fast and efficient model."},
]}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_codex_home(tmp_path, port=None):
    home = tmp_path / "codex"
    home.mkdir(exist_ok=True)
    config = 'model = "gpt-5.6-sol"\n'
    if port:
        config = 'openai_base_url = "http://127.0.0.1:{}/v1"\n'.format(port) + config
    (home / "config.toml").write_text(config)
    (home / "models_cache.json").write_text(json.dumps(CATALOG))
    return home


def base_env(tmp_path, port, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MODELMATCH_", "CODEX", "CLAUDE"))}
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
        "MODELMATCH_LOG_PROMPTS": "0",
        "MODELMATCH_NO_LAUNCHD": "1",
    })
    env.update({k: str(v) for k, v in extra.items()})
    return env


def get_json(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


@pytest.fixture
def proxy(tmp_path):
    """A real agents proxy on a free port, with isolated state, logs and Codex home (routing off)."""
    port = free_port()
    make_codex_home(tmp_path)
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


def run_hook(payload, env, timeout=30, **popen):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True, text=True, env=env,
                            timeout=timeout, **popen)
    result.elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    if result.stdout:
        message = json.loads(result.stdout)
        assert set(message) == {"systemMessage"}
        result.message = message["systemMessage"]
    else:
        result.message = None
    return result


_turns = itertools.count(1)


def prompt_event(prompt="What is the capital of France?", turn_id=None, model="gpt-5.6-sol"):
    return {"session_id": SESSION, "turn_id": turn_id or "turn-{}".format(next(_turns)), "cwd": str(ROOT),
            "hook_event_name": "UserPromptSubmit", "model": model, "permission_mode": "default", "prompt": prompt,
            "transcript_path": None}


def session_state(proxy):
    return get_json(proxy["url"] + "/session/codex/" + SESSION)["session"]


def routing_env(proxy, **extra):
    make_codex_home(proxy["tmp"], proxy["port"])  # openai_base_url -> this proxy
    return base_env(proxy["tmp"], proxy["port"], **extra)


# -- SessionStart -------------------------------------------------------------------


def test_session_start_registers_session(proxy):
    result = run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup",
                       "model": "gpt-5.6-sol", "cwd": str(ROOT)}, base_env(proxy["tmp"], proxy["port"]))
    assert result.message == "ModelMatch is on (recommendations only, routing off)."
    assert session_state(proxy)["start_model"] == "gpt-5.6-sol"


def test_session_start_reports_routing(proxy):
    result = run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup"},
                      routing_env(proxy))
    assert result.message == "ModelMatch is on (model routing active)."


def test_session_start_fails_open_when_proxy_cannot_start(tmp_path):
    make_codex_home(tmp_path)
    env = base_env(tmp_path, free_port(), MODELMATCH_PYTHON=str(tmp_path / "missing-python"))
    result = run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup"}, env)
    assert "couldn't start" in result.message


# -- UserPromptSubmit ------------------------------------------------------------------


def test_recommendation_only_when_routing_is_off(proxy):
    prompt = "Secret codename BLUEBIRD: what is 2+2?"
    result = run_hook(prompt_event(prompt), base_env(proxy["tmp"], proxy["port"]))
    assert "recommends GPT-5.6-Luna" in result.message
    assert "Recommendation only: model routing is off for Codex" in result.message
    log = (proxy["tmp"] / "logs" / "codex-hook.log").read_text()
    assert "BLUEBIRD" not in log and "BLUEBIRD" not in (proxy["tmp"] / "logs" / "agents-proxy.log").read_text()


def test_accept_applies_route_for_that_turn(proxy):
    event = prompt_event(turn_id="turn-accept")
    result = run_hook(event, routing_env(proxy, MODELMATCH_CONFIRM_UI="auto-accept"))
    assert "using GPT-5.6-Luna for this prompt" in result.message
    state = session_state(proxy)
    assert state["active_route"] == "gpt-5.6-luna"
    assert state["turn"]["prompt_id"] == "turn-accept" and state["turn"]["accepted"] is True


def test_reject_keeps_current_model(proxy):
    run_hook(prompt_event(), routing_env(proxy, MODELMATCH_CONFIRM_UI="auto-accept"))
    result = run_hook(prompt_event(), routing_env(proxy, MODELMATCH_CONFIRM_UI="never"))
    assert "kept your current model (recommended GPT-5.6-Luna)" in result.message
    assert session_state(proxy)["active_route"] is None


def test_already_current_model_needs_no_question(proxy):
    result = run_hook(prompt_event(model="gpt-5.6-luna"), routing_env(proxy, MODELMATCH_CONFIRM_UI="auto"))
    assert result.message == "ModelMatch recommends GPT-5.6-Luna, which is already your current model."


def test_unknown_model_is_shown_but_not_applied(proxy):
    result = run_hook(prompt_event("wc-test: Claude Opus 4.8"),
                      routing_env(proxy, MODELMATCH_CONFIRM_UI="auto-accept"))
    assert "recommends Claude Opus 4.8" in result.message
    assert "isn't in your Codex model list" in result.message
    assert session_state(proxy)["active_route"] is None


def test_slash_commands_and_disabled_flags_are_ignored(proxy):
    assert run_hook(prompt_event("/model"), base_env(proxy["tmp"], proxy["port"])).message is None
    for flag in ("MODELMATCH_DISABLED", "MODELMATCH_CODEX_DISABLED"):
        env = base_env(proxy["tmp"], proxy["port"], **{flag: "1"})
        assert run_hook(prompt_event(), env).message is None


def test_no_terminal_falls_back_to_current_model(proxy):
    result = run_hook(prompt_event(), routing_env(proxy, MODELMATCH_CONFIRM_UI="tty"), start_new_session=True)
    assert "couldn't be shown" in result.message
    assert session_state(proxy)["active_route"] is None


@pytest.fixture
def fake_osascript(tmp_path):
    """Stand-in for macOS `osascript` so tests never open real popups."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "osascript"
    script.write_text("""#!/bin/bash
while [ "$1" != "--" ]; do shift; done; shift
case "$FAKE_POPUP" in
  use)     echo "BUTTON:$2" ;;
  keep)    echo "execution error: User canceled. (-128)" >&2; exit 1 ;;
  timeout) echo "TIMEOUT" ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS popup")
@pytest.mark.parametrize("answer, expected_message, expected_route", [
    ("use", "using GPT-5.6-Luna", "gpt-5.6-luna"),
    ("keep", "kept your current model", None),
    ("timeout", "no answer within", None),
])
def test_popup_confirmation(proxy, fake_osascript, answer, expected_message, expected_route):
    env = routing_env(proxy, MODELMATCH_CONFIRM_UI="auto", FAKE_POPUP=answer)
    env["PATH"] = str(fake_osascript) + os.pathsep + env.get("PATH", "")
    result = run_hook(prompt_event(), env, start_new_session=True)
    assert expected_message in result.message
    assert session_state(proxy)["active_route"] == expected_route


# -- failures ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["", "not json", "[1, 2, 3]", json.dumps({"hook_event_name": "Mystery"}),
                                     json.dumps({"hook_event_name": "UserPromptSubmit"})])
def test_malformed_input_never_breaks_codex(proxy, payload):
    assert run_hook(payload, base_env(proxy["tmp"], proxy["port"])).message is None


def test_proxy_down_fails_open_quickly(tmp_path):
    make_codex_home(tmp_path)
    env = base_env(tmp_path, free_port(), MODELMATCH_PYTHON=str(tmp_path / "missing-python"))
    result = run_hook(prompt_event(), env)
    assert "unavailable right now" in result.message
    assert result.elapsed < 5


def test_missing_hook_file_cannot_block_codex(tmp_path):
    # Codex treats exit code 2 as "block the prompt"; the installed command ends in || true.
    result = subprocess.run('python3 "{}" || true'.format(tmp_path / "gone.py"), shell=True, input="{}",
                            capture_output=True, text=True)
    assert result.returncode == 0


def test_duplicate_hook_invocations_act_only_once(proxy):
    """User-level and project-level hooks.json both run for the same prompt (concurrently, in Codex)."""
    env = routing_env(proxy, MODELMATCH_CONFIRM_UI="auto-accept")
    event = prompt_event(turn_id="same-turn")
    first = run_hook(event, env)
    second = run_hook(event, env)
    assert "using GPT-5.6-Luna" in first.message
    assert second.message is None
    assert "already handled by another copy" in (proxy["tmp"] / "logs" / "codex-hook.log").read_text()


def test_codex_exec_runs_are_skipped_unless_auto_accept(proxy):
    sys.path.insert(0, str(ROOT / "hooks"))
    import modelmatch_codex_hook as hook
    assert hook.codex_subcommand("/opt/tools/bin/codex exec --skip-git-repo-check say hi") == "exec"
    assert hook.codex_subcommand("codex -m gpt-5.5 -c a=b e hi") == "e"
    assert hook.codex_subcommand("codex please exec this") == "please"
    assert hook.codex_subcommand("/Applications/X.app/Contents/MacOS/codex app-server") == "app-server"
    assert hook.codex_subcommand("node something-else") is None

    # A real `codex exec`-like parent: a shell script named codex that runs the hook.
    fake = proxy["tmp"] / "bin" / "codex"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text('#!/bin/bash\n"{}" "{}"\n'.format(sys.executable, HOOK))
    fake.chmod(0o755)
    env = routing_env(proxy, MODELMATCH_CONFIRM_UI="auto")
    result = subprocess.run([str(fake), "exec", "do it"], input=json.dumps(prompt_event()), capture_output=True,
                            text=True, env=env, timeout=30)
    assert result.returncode == 0 and result.stdout == ""
    assert "non-interactive run" in (proxy["tmp"] / "logs" / "codex-hook.log").read_text()


def test_paused_hook_still_keeps_the_proxy_alive_when_routing(tmp_path):
    port = free_port()
    make_codex_home(tmp_path, port)
    env = base_env(tmp_path, port, MODELMATCH_DISABLED="1")
    try:
        result = run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup"}, env)
        assert result.message is None
        assert get_json("http://127.0.0.1:{}/health".format(port))["service"] == "modelmatch-agents-proxy"
    finally:
        subprocess.run([sys.executable, str(HOOK), "--stop-proxy"], env=env, capture_output=True, timeout=20)


def test_command_line_helpers(tmp_path):
    port = free_port()
    make_codex_home(tmp_path)
    env = base_env(tmp_path, port)
    try:
        assert subprocess.run([sys.executable, str(HOOK), "--status"], env=env, capture_output=True).returncode == 1
        started = subprocess.run([sys.executable, str(HOOK), "--start-proxy"], env=env, capture_output=True, text=True,
                                 timeout=30)
        assert started.returncode == 0 and "running" in started.stdout
        status = subprocess.run([sys.executable, str(HOOK), "--status"], env=env, capture_output=True, text=True)
        assert json.loads(status.stdout)["service"] == "modelmatch-agents-proxy"
    finally:
        stopped = subprocess.run([sys.executable, str(HOOK), "--stop-proxy"], env=env, capture_output=True, text=True,
                                 timeout=20)
        assert "stopped" in stopped.stdout
