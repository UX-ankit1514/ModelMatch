"""settings_tool.py against a fake home/project, never the real ~/.claude."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "scripts" / "settings_tool.py"
MARKER = "modelmatch_hook.py"


def tool(*args, home, extra_env=None):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "MODELMATCH_PORT": "8787"}
    env.update(extra_env or {})
    result = subprocess.run([sys.executable, str(TOOL)] + [str(a) for a in args], capture_output=True, text=True,
                            env=env)
    return result.returncode, result.stdout


def read(path):
    return json.loads(Path(path).read_text())


@pytest.fixture
def home(tmp_path):
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    return tmp_path / "home"


def ours(settings):
    return [h for event in settings.get("hooks", {}).values() for g in event for h in g["hooks"] if MARKER in h["command"]]


def test_global_install_merges_without_touching_other_settings(home):
    path = home / ".claude" / "settings.json"
    original = {"model": "opus", "enabledPlugins": {"x@y": True}, "permissions": {"allow": ["Bash(ls)"]},
                "env": {"FOO": "bar"},
                "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
    path.write_text(json.dumps(original))

    code, out = tool("install", "--global", home=home)
    assert code == 0, out
    settings = read(path)
    assert settings["model"] == "opus" and settings["enabledPlugins"] == {"x@y": True}
    assert settings["permissions"] == original["permissions"]
    assert settings["env"] == {"FOO": "bar", "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}
    assert len(ours(settings)) == 2
    assert {"type": "command", "command": "echo mine"} in settings["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert list((home / ".claude").glob("settings.json.bak-*"))  # backup made

    # idempotent: a second install changes nothing and adds no duplicates
    before = path.read_text()
    code, out = tool("install", "--global", home=home)
    assert code == 0 and path.read_text() == before and len(ours(read(path))) == 2

    # status sees it
    code, out = tool("status", "--global", home=home)
    assert json.loads(out)["hook_events"] == ["SessionStart", "UserPromptSubmit"]

    # uninstall restores exactly what was there before
    code, out = tool("uninstall", "--global", home=home)
    assert code == 0
    assert read(path) == original


def test_hook_command_can_never_block_claude(home):
    tool("install", "--global", home=home)
    command = ours(read(home / ".claude" / "settings.json"))[0]["command"]
    assert command.endswith("|| true")
    # run it exactly as Claude Code would (via the shell) with the hook file missing
    broken = command.replace(str(ROOT / "hooks"), "/definitely/missing")
    result = subprocess.run(["sh", "-c", broken], input="{}", capture_output=True, text=True)
    assert result.returncode == 0


def test_global_install_creates_settings_when_none_exist(tmp_path):
    home = tmp_path / "fresh-home"
    home.mkdir()
    code, _ = tool("install", "--global", home=home)
    assert code == 0 and len(ours(read(home / ".claude" / "settings.json"))) == 2


def test_no_routing_flag_leaves_base_url_unset(home):
    tool("install", "--global", "--no-routing", home=home)
    settings = read(home / ".claude" / "settings.json")
    assert "ANTHROPIC_BASE_URL" not in settings.get("env", {})
    assert len(ours(settings)) == 2


def test_existing_base_url_is_never_overwritten(home):
    path = home / ".claude" / "settings.json"
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://my-gateway.example"}}))
    code, out = tool("install", "--global", home=home)
    assert code == 0 and "NOT enabling routing" in out
    assert read(path)["env"]["ANTHROPIC_BASE_URL"] == "https://my-gateway.example"
    tool("uninstall", "--global", home=home)
    assert read(path)["env"]["ANTHROPIC_BASE_URL"] == "https://my-gateway.example"  # not ours: left alone


def test_shell_startup_file_base_url_blocks_global_routing(home):
    (home / ".zshrc").write_text("# ANTHROPIC_BASE_URL=commented-out\nexport PATH=$PATH\n")
    code, out = tool("install", "--global", home=home)
    assert "NOT enabling routing" not in out  # a comment doesn't count
    tool("uninstall", "--global", home=home)

    (home / ".zshrc").write_text("export ANTHROPIC_BASE_URL=https://gateway.example\n")
    code, out = tool("install", "--global", home=home)
    assert "NOT enabling routing" in out and ".zshrc" in out
    settings = read(home / ".claude" / "settings.json")
    assert "ANTHROPIC_BASE_URL" not in settings.get("env", {}) and len(ours(settings)) == 2


def test_invalid_settings_file_is_never_overwritten(home):
    path = home / ".claude" / "settings.json"
    path.write_text("{ not valid json")
    code, out = tool("install", "--global", home=home)
    assert code == 1 and "no settings were changed" in out
    assert path.read_text() == "{ not valid json"


def test_claude_config_dir_is_respected(tmp_path):
    custom = tmp_path / "elsewhere"
    code, _ = tool("install", "--global", home=tmp_path, extra_env={"CLAUDE_CONFIG_DIR": str(custom)})
    assert code == 0 and len(ours(read(custom / "settings.json"))) == 2
    assert not (tmp_path / ".claude").exists()


def test_project_install_keeps_routing_in_local_file(home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    code, _ = tool("install", project, home=home)
    assert code == 0
    assert len(ours(read(project / ".claude" / "settings.json"))) == 2
    assert read(project / ".claude" / "settings.local.json")["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"
    tool("uninstall", project, home=home)
    assert ours(read(project / ".claude" / "settings.json")) == []
    assert read(project / ".claude" / "settings.local.json") == {}
