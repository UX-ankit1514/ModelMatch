"""End-to-end tests of hooks/modelmatch_hook.py against a real proxy process.

Every scenario checks the fail-open contract: exit code 0, and stdout is either
empty or a single JSON object with a "systemMessage".
"""

import itertools
import json
import os
import select
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "modelmatch_hook.py"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
SESSION = "5e55a0b1-0000-4000-8000-00000000abcd"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def base_env(tmp_path, port, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MODELMATCH_", "ANTHROPIC_BASE_URL", "CLAUDE"))}
    env.update({
        "MODELMATCH_PORT": str(port),
        "MODELMATCH_LOG_DIR": str(tmp_path / "logs"),
        "MODELMATCH_STATE_DIR": str(tmp_path / "state"),
        "MODELMATCH_STATE_FILE": str(tmp_path / "state" / "sessions.json"),
        "MODELMATCH_API_URL": "",
        "MODELMATCH_CONFIRM_UI": "never",
        "MODELMATCH_CONFIRM_TIMEOUT": "3",
        "MODELMATCH_DISABLED": "0",
        "MODELMATCH_LOG_PROMPTS": "0",
    })
    env.update({k: str(v) for k, v in extra.items()})
    return env


def get_json(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


@pytest.fixture
def proxy(tmp_path):
    """A real proxy process on a free port with isolated state/logs."""
    port = free_port()
    env = base_env(tmp_path, port)
    process = subprocess.Popen([str(VENV_PYTHON), "-m", "uvicorn", "proxy.app:create_app", "--factory",
                                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
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


_prompt_ids = itertools.count(1)


def prompt_event(prompt="What is the capital of France?", prompt_id=None):
    return {"session_id": SESSION, "cwd": str(ROOT), "hook_event_name": "UserPromptSubmit", "prompt": prompt,
            "prompt_id": prompt_id or "p-{}".format(next(_prompt_ids))}


def session_state(proxy):
    return get_json(proxy["url"] + "/session/" + SESSION)["session"]


def routing_env(proxy, **extra):
    return base_env(proxy["tmp"], proxy["port"], ANTHROPIC_BASE_URL=proxy["url"], **extra)


# -- SessionStart -------------------------------------------------------------------


def test_session_start_registers_session(proxy):
    result = run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup",
                       "model": "claude-opus-5-5"}, base_env(proxy["tmp"], proxy["port"]))
    assert "ModelMatch is on" in result.message
    assert session_state(proxy)["start_model"] == "claude-opus-5-5"


def test_session_start_fails_open_when_proxy_cannot_start(tmp_path):
    env = base_env(tmp_path, free_port(), MODELMATCH_PYTHON=str(tmp_path / "missing-python"))
    result = run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup"}, env)
    assert "couldn't start" in result.message


# -- UserPromptSubmit: detection & recommendation ---------------------------------------


def test_prompt_detected_and_logged_safely(proxy):
    prompt = "Secret codename BLUEBIRD: what is 2+2?"
    result = run_hook(prompt_event(prompt), base_env(proxy["tmp"], proxy["port"]))
    assert "recommends Claude Haiku 4.5" in result.message
    assert "Recommendation only" in result.message  # routing is off without ANTHROPIC_BASE_URL
    import hashlib
    fp = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    hook_log = (proxy["tmp"] / "logs" / "hook.log").read_text()
    assert "prompt={} chars fp={}".format(len(prompt), fp) in hook_log
    assert "BLUEBIRD" not in hook_log
    assert "BLUEBIRD" not in (proxy["tmp"] / "logs" / "proxy.log").read_text()


def test_accept_applies_route(proxy):
    result = run_hook(prompt_event(), routing_env(proxy, MODELMATCH_CONFIRM_UI="auto-accept"))
    assert "using Claude Haiku 4.5 for this prompt" in result.message
    state = session_state(proxy)
    assert state["active_route"] == "claude-haiku-4-5"
    assert state["turn"]["accepted"] is True


def test_reject_keeps_current_model(proxy):
    run_hook(prompt_event(), routing_env(proxy, MODELMATCH_CONFIRM_UI="auto-accept"))
    result = run_hook(prompt_event("Fix this bug in my python code"),
                      routing_env(proxy, MODELMATCH_CONFIRM_UI="never"))
    assert "kept your current model (recommended Claude Sonnet 5.5)" in result.message
    assert session_state(proxy)["active_route"] is None  # previous turn's route cleared


def test_unsupported_model_is_shown_but_not_applied(proxy):
    result = run_hook(prompt_event("wc-test: GPT-4o"), routing_env(proxy, MODELMATCH_CONFIRM_UI="auto-accept"))
    assert "recommends GPT-4o" in result.message
    assert "isn't set up yet" in result.message
    assert session_state(proxy)["active_route"] is None


def test_slash_commands_and_disabled_flag_are_ignored(proxy):
    assert run_hook(prompt_event("/model"), base_env(proxy["tmp"], proxy["port"])).message is None
    env = base_env(proxy["tmp"], proxy["port"], MODELMATCH_DISABLED="1")
    assert run_hook(prompt_event(), env).message is None


# -- confirmation UI ------------------------------------------------------------------


def test_no_terminal_falls_back_to_current_model(proxy):
    # start_new_session=True: no controlling terminal, exactly like Claude Code's hooks.
    result = run_hook(prompt_event(), routing_env(proxy, MODELMATCH_CONFIRM_UI="tty"),
                      start_new_session=True)
    assert "couldn't be shown" in result.message
    assert session_state(proxy)["active_route"] is None
    assert "confirmation UI unavailable: no terminal" in (proxy["tmp"] / "logs" / "hook.log").read_text()


def run_hook_in_terminal(env, keys):
    """Run the hook with a pseudo-terminal as its controlling terminal and type `keys` at the prompt."""
    import fcntl
    import termios

    master, slave = os.openpty()
    slave_name = os.ttyname(slave)

    def make_controlling_terminal():
        os.setsid()
        fd = os.open(slave_name, os.O_RDWR)
        fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
        os.close(fd)

    process = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, preexec_fn=make_controlling_terminal)
    process.stdin.write(json.dumps(prompt_event()).encode())
    process.stdin.close()
    process.stdin = None  # so communicate() doesn't try to flush it
    screen = bytearray()
    stop = threading.Event()

    def read_screen():  # like a terminal window, keep displaying output
        while not stop.is_set():
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                screen.extend(chunk)

    reader = threading.Thread(target=read_screen, daemon=True)
    reader.start()
    deadline = time.monotonic() + 15
    while b"[Y/n]" not in screen and time.monotonic() < deadline:
        time.sleep(0.05)
    if keys:
        os.write(master, keys)
    out, err = process.communicate(timeout=20)
    time.sleep(0.2)
    stop.set()
    reader.join(timeout=2)
    os.close(master)
    os.close(slave)
    assert process.returncode == 0, err
    return json.loads(out)["systemMessage"], bytes(screen).decode("utf-8", "replace")


@pytest.mark.parametrize("keys, expected_message, expected_route", [
    (b"y", "using Claude Haiku 4.5", "claude-haiku-4-5"),
    (b"\r", "using Claude Haiku 4.5", "claude-haiku-4-5"),  # Enter = default Yes
    (b"n", "kept your current model", None),
    (b"", "no answer within", None),  # timeout
])
def test_terminal_confirmation(proxy, keys, expected_message, expected_route):
    env = routing_env(proxy, MODELMATCH_CONFIRM_UI="tty", MODELMATCH_CONFIRM_TIMEOUT="2")
    message, screen = run_hook_in_terminal(env, keys)
    assert "Recommended model: Claude Haiku 4.5" in screen
    assert "Use recommended model? [Y/n]" in screen
    assert expected_message in message
    assert session_state(proxy)["active_route"] == expected_route


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
  broken)  echo "execution error: not allowed" >&2; exit 1 ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS popup")
@pytest.mark.parametrize("answer, expected_message, expected_route", [
    ("use", "using Claude Haiku 4.5", "claude-haiku-4-5"),
    ("keep", "kept your current model", None),
    ("timeout", "no answer within", None),
    ("broken", "couldn't be shown", None),
])
def test_popup_confirmation(proxy, fake_osascript, answer, expected_message, expected_route):
    env = routing_env(proxy, MODELMATCH_CONFIRM_UI="auto", FAKE_POPUP=answer)
    env["PATH"] = str(fake_osascript) + os.pathsep + env.get("PATH", "")
    # auto = terminal first (unavailable in a new session), then the popup
    result = run_hook(prompt_event(), env, start_new_session=True)
    assert expected_message in result.message
    assert session_state(proxy)["active_route"] == expected_route


# -- failures (Milestone 9) ---------------------------------------------------------------


@pytest.mark.parametrize("payload", ["", "not json", "[1, 2, 3]", json.dumps({"hook_event_name": "Mystery"}),
                                     json.dumps({"hook_event_name": "UserPromptSubmit"})])
def test_malformed_input_never_breaks_claude(proxy, payload):
    assert run_hook(payload, base_env(proxy["tmp"], proxy["port"])).message is None


def test_proxy_down_fails_open_quickly(tmp_path):
    env = base_env(tmp_path, free_port(), MODELMATCH_PYTHON=str(tmp_path / "missing-python"),
                   ANTHROPIC_BASE_URL="http://127.0.0.1:1")
    result = run_hook(prompt_event(), env)
    assert "unavailable right now" in result.message
    assert result.elapsed < 5


def test_cloud_router_down_fails_open(tmp_path):
    port = free_port()
    env = base_env(tmp_path, port, MODELMATCH_API_URL="http://127.0.0.1:9/api/route",
                   MODELMATCH_API_TIMEOUT="2", ANTHROPIC_BASE_URL="http://127.0.0.1:{}".format(port),
                   MODELMATCH_CONFIRM_UI="auto-accept")
    try:
        result = run_hook(prompt_event(), env)  # the hook starts its own proxy here
        assert "couldn't get a recommendation" in result.message
        assert "keeping current model" in (tmp_path / "logs" / "hook.log").read_text()
    finally:
        subprocess.run([sys.executable, str(HOOK), "--stop-proxy"], env=env, capture_output=True, timeout=20)


def test_slow_cloud_router_times_out_and_fails_open(tmp_path):
    # A "cloud" that accepts the connection but never answers.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    slow_url = "http://127.0.0.1:{}/api/route".format(server.getsockname()[1])
    port = free_port()
    env = base_env(tmp_path, port, MODELMATCH_API_URL=slow_url, MODELMATCH_API_TIMEOUT="1",
                   ANTHROPIC_BASE_URL="http://127.0.0.1:{}".format(port))
    try:
        result = run_hook(prompt_event(), env)
        assert "timed out" in result.message
        assert result.elapsed < 15
    finally:
        subprocess.run([sys.executable, str(HOOK), "--stop-proxy"], env=env, capture_output=True, timeout=20)
        server.close()


# -- running from several places at once (global + project settings) ----------------


def test_duplicate_hook_invocations_act_only_once(proxy):
    """User-level and project-level settings can both call the hook for the same prompt."""
    env = routing_env(proxy, MODELMATCH_CONFIRM_UI="auto-accept")
    event = prompt_event(prompt_id="same-prompt")
    first = run_hook(event, env)
    second = run_hook(event, env)
    assert "using Claude Haiku 4.5" in first.message
    assert second.message is None
    log = (proxy["tmp"] / "logs" / "hook.log").read_text()
    assert log.count("recommendation request") == 1
    assert "already handled by another copy" in log
    # a genuinely new prompt is still handled
    assert run_hook(prompt_event(), env).message is not None


def test_session_start_duplicates_are_ignored(proxy):
    env = base_env(proxy["tmp"], proxy["port"])
    event = {"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup"}
    assert run_hook(event, env).message is not None
    assert run_hook(event, env).message is None


def test_headless_runs_are_skipped_unless_explicitly_enabled(proxy):
    env = routing_env(proxy, MODELMATCH_CONFIRM_UI="auto", CLAUDE_CODE_ENTRYPOINT="sdk-cli")
    assert run_hook(prompt_event(), env).message is None  # no popup, no routing, nothing
    assert "non-interactive" in (proxy["tmp"] / "logs" / "hook.log").read_text()
    assert session_state_or_none(proxy) is None
    env["MODELMATCH_CONFIRM_UI"] = "auto-accept"  # explicit opt-in for automation
    assert "using Claude Haiku 4.5" in run_hook(prompt_event(), env).message


def session_state_or_none(proxy):
    try:
        return session_state(proxy)
    except OSError:
        return None


def test_paused_hook_still_keeps_the_proxy_alive_when_routing(tmp_path):
    """Paused + routing on + proxy down must not leave Claude without its API route."""
    port = free_port()
    env = base_env(tmp_path, port, MODELMATCH_DISABLED="1",
                   ANTHROPIC_BASE_URL="http://127.0.0.1:{}".format(port))
    try:
        result = run_hook({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup"}, env)
        assert result.message is None  # paused: quiet
        assert get_json("http://127.0.0.1:{}/health".format(port))["status"] == "ok"
        assert run_hook(prompt_event(), env).message is None  # no recommendation while paused
    finally:
        subprocess.run([sys.executable, str(HOOK), "--stop-proxy"], env=env, capture_output=True, timeout=20)


def test_pause_and_resume_scripts(tmp_path):
    import shutil
    copy = tmp_path / "repo"
    (copy / "scripts").mkdir(parents=True)
    for name in ("pause.sh", "resume.sh", "set_env.py"):
        shutil.copy2(str(ROOT / "scripts" / name), str(copy / "scripts" / name))
    (copy / ".env.example").write_text("MODELMATCH_PORT=8787\nMODELMATCH_DISABLED=0\n")
    subprocess.run(["bash", str(copy / "scripts" / "pause.sh")], check=True, capture_output=True)
    text = (copy / ".env").read_text()
    assert "MODELMATCH_DISABLED=1" in text and "MODELMATCH_PORT=8787" in text
    subprocess.run(["bash", str(copy / "scripts" / "resume.sh")], check=True, capture_output=True)
    assert (copy / ".env").read_text().count("MODELMATCH_DISABLED") == 1
    assert "MODELMATCH_DISABLED=0" in (copy / ".env").read_text()
