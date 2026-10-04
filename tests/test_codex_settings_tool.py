"""codex_settings_tool.py against a fake home / Codex folder, never the real ~/.codex."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "scripts" / "codex_settings_tool.py"
MARKER = "modelmatch_codex_hook.py"
OURS = "http://127.0.0.1:8788/v1"


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    repo_env = tmp_path / "repo-env"  # where set_env.py would write; isolated via a copy of the scripts
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "CODEX_HOME": str(home / ".codex"),
            "MODELMATCH_AGENTS_PORT": "8788", "MODELMATCH_CODEX_UPSTREAM_URL": "",
            "_tmp": str(tmp_path), "_repo_env": str(repo_env)}


@pytest.fixture
def repo_copy(tmp_path):
    """A copy of the parts of the repo the tool uses, so .env writes never touch the real .env."""
    import shutil
    copy = tmp_path / "repo"
    for folder in ("scripts", "proxy", "hooks"):
        shutil.copytree(str(ROOT / folder), str(copy / folder), ignore=shutil.ignore_patterns("__pycache__"))
    (copy / ".env.example").write_text("MODELMATCH_PORT=8787\n")
    return copy


def tool(repo, env, *args):
    run_env = {k: v for k, v in env.items() if not k.startswith("_")}
    result = subprocess.run([sys.executable, str(repo / "scripts" / "codex_settings_tool.py")] + [str(a) for a in args],
                            capture_output=True, text=True, env=run_env)
    return result.returncode, result.stdout


def codex(env):
    return Path(env["CODEX_HOME"])


def ours(hooks):
    return [h for groups in hooks.get("hooks", {}).values() for g in groups for h in g["hooks"] if MARKER in h["command"]]


def test_global_install_merges_and_is_idempotent(repo_copy, env):
    path = codex(env) / "hooks.json"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "echo mine"}]}]}}))
    code, out = tool(repo_copy, env, "install", "--global")
    assert code == 0, out
    data = json.loads(path.read_text())
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo mine"
    assert len(ours(data)) == 2
    start = data["hooks"]["SessionStart"][0]
    assert start["matcher"] == "startup|resume|clear" and start["hooks"][0]["timeout"] == 30
    prompt = data["hooks"]["UserPromptSubmit"][0]
    assert "matcher" not in prompt and prompt["hooks"][0]["timeout"] == 120
    assert prompt["hooks"][0]["command"].endswith('" || true')
    assert list(codex(env).glob("hooks.json.bak-*"))

    code, out = tool(repo_copy, env, "install", "--global")
    assert "already up to date" in out and len(ours(json.loads(path.read_text()))) == 2


def test_project_install_and_uninstall(repo_copy, env, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert tool(repo_copy, env, "install", project)[0] == 0
    assert len(ours(json.loads((project / ".codex" / "hooks.json").read_text()))) == 2
    code, out = tool(repo_copy, env, "uninstall", project)
    assert "removed 2" in out
    assert json.loads((project / ".codex" / "hooks.json").read_text()) == {}


def test_invalid_hooks_file_is_never_overwritten(repo_copy, env):
    path = codex(env) / "hooks.json"
    path.write_text("{ not json")
    code, out = tool(repo_copy, env, "install", "--global")
    assert code == 1 and "no settings were changed" in out
    assert path.read_text() == "{ not json"


def test_routing_on_and_off_without_previous_url(repo_copy, env):
    config = codex(env) / "config.toml"
    original = 'model = "gpt-5.6-sol"\nnotify = [\n  "a",\n]\n\n[features]\nhooks = true\n'
    config.write_text(original)
    code, out = tool(repo_copy, env, "routing", "on")
    assert code == 0 and "model routing on" in out
    text = config.read_text()
    assert 'openai_base_url = "{}"'.format(OURS) in text and text.endswith(original)
    assert tool(repo_copy, env, "routing", "on")[1].strip().endswith("already on ({})".format(OURS))
    status = json.loads(tool(repo_copy, env, "status", "--global")[1])
    assert status["routing_on"] is True

    code, out = tool(repo_copy, env, "routing", "off")
    assert "routing off" in out and config.read_text() == original


def test_routing_keeps_and_restores_a_previous_gateway(repo_copy, env):
    config = codex(env) / "config.toml"
    original = 'model = "gpt-5.6-sol"\nopenai_base_url = "http://127.0.0.1:10100/v1"\n[features]\nhooks = true\n'
    config.write_text(original)
    code, out = tool(repo_copy, env, "routing", "on")
    assert "previous openai_base_url" in out and "10100" in out
    assert "MODELMATCH_CODEX_UPSTREAM_URL=http://127.0.0.1:10100/v1" in (repo_copy / ".env").read_text()
    text = config.read_text()
    assert 'openai_base_url = "{}"'.format(OURS) in text and "10100" in text  # kept in a marker comment
    code, out = tool(repo_copy, env, "routing", "off")
    assert "restored to http://127.0.0.1:10100/v1" in out
    assert config.read_text() == original


def test_prepare_upstream_records_the_current_gateway(repo_copy, env):
    (codex(env) / "config.toml").write_text('openai_base_url = "https://gateway.example/v1/"\n')
    code, out = tool(repo_copy, env, "prepare-upstream")
    assert code == 0 and "gateway.example" in out
    assert "MODELMATCH_CODEX_UPSTREAM_URL=https://gateway.example/v1" in (repo_copy / ".env").read_text()


def test_routing_refused_for_custom_provider_or_shell_override(repo_copy, env):
    config = codex(env) / "config.toml"
    config.write_text('model_provider = "ollama"\n')
    code, out = tool(repo_copy, env, "routing", "on")
    assert code == 0 and "NOT enabling routing" in out and "openai_base_url" not in config.read_text()

    config.write_text('model = "x"\n')
    (Path(env["HOME"]) / ".zshrc").write_text("export OPENAI_BASE_URL=https://elsewhere\n")
    code, out = tool(repo_copy, env, "routing", "on")
    assert "NOT enabling routing" in out and ".zshrc" in out and config.read_text() == 'model = "x"\n'


def test_routing_requires_a_healthy_proxy_when_asked(repo_copy, env):
    (codex(env) / "config.toml").write_text('model = "x"\n')
    env = dict(env, MODELMATCH_AGENTS_PORT="1")  # nothing listens there
    code, out = tool(repo_copy, env, "routing", "on", "--require-proxy")
    assert code == 1 and "isn't answering" in out
    assert (codex(env) / "config.toml").read_text() == 'model = "x"\n'


def test_routing_off_never_touches_someone_elses_url(repo_copy, env):
    config = codex(env) / "config.toml"
    config.write_text('openai_base_url = "https://gateway.example/v1"\n')
    code, out = tool(repo_copy, env, "routing", "off")
    assert "was not on" in out and config.read_text() == 'openai_base_url = "https://gateway.example/v1"\n'


def test_status_reports_disabled_hooks(repo_copy, env):
    (codex(env) / "config.toml").write_text("[features]\nhooks = false\n")
    status = json.loads(tool(repo_copy, env, "status", "--global")[1])
    assert status["hooks_feature_disabled"] is True and status["hook_events"] == []
    code, out = tool(repo_copy, env, "install", "--global")
    assert "hooks switched off" in out
