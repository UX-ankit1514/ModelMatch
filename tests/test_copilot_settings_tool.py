"""copilot_settings_tool.py against a fake ~/.copilot, never the real one."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "scripts" / "copilot_settings_tool.py"


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".copilot").mkdir(parents=True)
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "COPILOT_HOME": str(home / ".copilot"),
            "MODELMATCH_STATE_DIR": str(tmp_path / "state")}


def tool(env, *args):
    result = subprocess.run([sys.executable, str(TOOL)] + list(args), capture_output=True, text=True, env=env)
    return result.returncode, result.stdout


def copilot(env):
    return Path(env["COPILOT_HOME"])


def status(env):
    return json.loads(tool(env, "status")[1])


def test_extension_install_turns_on_experimental_and_uninstall_reverts_it(env):
    (copilot(env) / "settings.json").write_text(json.dumps({"model": "claude-sonnet-4.6"}))
    code, out = tool(env, "install")
    assert code == 0, out
    folder = copilot(env) / "extensions" / "modelmatch"
    assert (folder / "extension.mjs").read_text() == (ROOT / "hooks" / "copilot-extension" / "extension.mjs").read_text()
    marker = json.loads((folder / "modelmatch.json").read_text())
    assert marker["repo"] == str(ROOT) and marker["python"]
    assert json.loads((copilot(env) / "settings.json").read_text()) == {"model": "claude-sonnet-4.6", "experimental": True}
    assert status(env)["extension_installed"] and status(env)["extension_up_to_date"] and status(env)["experimental"]

    code, out = tool(env, "uninstall")
    assert "removed the extension" in out and not folder.exists()
    assert json.loads((copilot(env) / "settings.json").read_text()) == {"model": "claude-sonnet-4.6"}


def test_experimental_already_on_is_left_on(env):
    (copilot(env) / "settings.json").write_text(json.dumps({"experimental": True}))
    tool(env, "install")
    tool(env, "uninstall")
    assert json.loads((copilot(env) / "settings.json").read_text()) == {"experimental": True}


def test_settings_with_comments_are_never_rewritten(env):
    original = '// mine\n{"model": "gpt-5.4"}\n'
    (copilot(env) / "settings.json").write_text(original)
    code, out = tool(env, "install")
    assert code == 0 and "/experimental on" in out
    assert (copilot(env) / "settings.json").read_text() == original
    assert status(env)["extension_installed"] is True


def test_hooks_only_install_replaces_the_extension(env):
    tool(env, "install")
    code, out = tool(env, "install", "--hooks-only")
    assert code == 0
    hooks = json.loads((copilot(env) / "hooks" / "modelmatch.json").read_text())
    assert hooks["version"] == 1
    assert hooks["hooks"]["userPromptSubmitted"][0]["bash"].endswith("--event userPromptSubmitted || true")
    assert not (copilot(env) / "extensions" / "modelmatch").exists()
    assert status(env)["hooks_only_installed"] is True

    tool(env, "install")  # and back
    assert not (copilot(env) / "hooks" / "modelmatch.json").exists()


def test_someone_elses_extension_folder_is_left_alone(env):
    folder = copilot(env) / "extensions" / "modelmatch"
    folder.mkdir(parents=True)
    (folder / "extension.mjs").write_text("// not ours")
    code, out = tool(env, "install")
    assert code == 1 and "isn't ModelMatch's" in out
    assert (folder / "extension.mjs").read_text() == "// not ours"
    tool(env, "uninstall")
    assert (folder / "extension.mjs").exists()
