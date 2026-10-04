"""agents_service.py: the macOS LaunchAgent for the agents proxy (launchd itself is skipped in tests).

The service runs from its own copy of the proxy, because macOS refuses to let background
services read files in Desktop / Documents / Downloads.
"""

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "scripts" / "agents_service.py"


@pytest.fixture
def env(tmp_path):
    return {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "MODELMATCH_NO_LAUNCHD": "1",
            "MODELMATCH_LAUNCHD_PLIST": str(tmp_path / "LaunchAgents" / "com.modelmatch.agents-proxy.plist"),
            "MODELMATCH_RUNTIME_DIR": str(tmp_path / "runtime"), "MODELMATCH_AGENTS_PORT": "8799",
            "MODELMATCH_LOG_DIR": str(tmp_path / "logs"), "MODELMATCH_STATE_DIR": str(tmp_path / "state")}


def run(env, *args):
    result = subprocess.run([sys.executable, str(SERVICE)] + list(args), capture_output=True, text=True, env=env)
    return result.returncode, result.stdout


def status(env):
    return json.loads(run(env, "status")[1])


def test_install_writes_plist_that_runs_from_its_own_copy(tmp_path, env):
    runtime = tmp_path / "runtime"
    code, out = run(env, "install")
    assert code == 0, out
    data = plistlib.loads(Path(env["MODELMATCH_LAUNCHD_PLIST"]).read_bytes())
    assert data["Label"] == "com.modelmatch.agents-proxy"
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    args = data["ProgramArguments"]
    assert args[0] == str(runtime / "venv" / "bin" / "python")
    assert args[1:5] == ["-m", "uvicorn", "proxy.agents.app:create_app", "--factory"]
    assert "8799" in args and args[-2:] == ["--ws", "none"]
    assert data["WorkingDirectory"] == str(runtime / "app")
    assert data["StandardOutPath"].startswith(str(runtime / "logs"))
    assert data["EnvironmentVariables"]["MODELMATCH_LOG_DIR"] == str(runtime / "logs")
    # nothing the service needs lives in the (possibly Desktop) project folder
    for value in [args[0], data["WorkingDirectory"], data["StandardOutPath"]]:
        assert str(ROOT) not in value
    assert (runtime / "app" / "proxy" / "agents" / "app.py").read_text() == (ROOT / "proxy" / "agents" / "app.py").read_text()
    assert not list((runtime / "app").rglob("__pycache__"))


def test_settings_copy_is_private_and_kept_current(tmp_path, env):
    runtime = tmp_path / "runtime"
    assert run(env, "install")[0] == 0
    copied = runtime / "app" / ".env"
    if (ROOT / ".env").exists():
        assert copied.read_text() == (ROOT / ".env").read_text()
        assert oct(copied.stat().st_mode & 0o777) == "0o600"  # it can hold the router key
    assert status(env)["runtime_current"] is True

    (runtime / "app" / "proxy" / "agents" / "app.py").write_text("# old copy\n")
    (runtime / "app" / "proxy" / "leftover.py").write_text("# removed upstream\n")
    assert status(env)["runtime_current"] is False
    code, out = run(env, "sync")
    assert code == 0 and "refreshed" in out
    assert status(env)["runtime_current"] is True
    assert not (runtime / "app" / "proxy" / "leftover.py").exists()
    assert "already up to date" in run(env, "sync")[1]


def test_uninstall_removes_plist_and_only_its_own_folder(tmp_path, env):
    runtime = tmp_path / "runtime"
    assert run(env, "install")[0] == 0
    assert json.loads(run(env, "status")[1])["installed"] is True
    code, out = run(env, "uninstall")
    assert code == 0 and not Path(env["MODELMATCH_LAUNCHD_PLIST"]).exists() and not runtime.exists()

    stranger = tmp_path / "somebody-elses-folder"  # not ours (no marker): never deleted
    stranger.mkdir()
    (stranger / "keep.txt").write_text("mine")
    env["MODELMATCH_RUNTIME_DIR"] = str(stranger)
    assert run(env, "uninstall")[0] == 0
    assert (stranger / "keep.txt").read_text() == "mine"


@pytest.mark.skipif(sys.platform != "darwin" or bool(os.environ.get("CI")), reason="needs a Mac; builds a real venv")
def test_service_environment_can_be_built_and_runs_the_proxy(tmp_path, env):
    """The part that failed on Desktop: the service's own venv + copy must start the proxy by themselves."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import agents_service as service

    runtime = tmp_path / "runtime"
    service.sync_app(runtime)
    # reuse the project's venv packages to keep this fast: link instead of downloading
    import venv
    venv.create(str(runtime / "venv"), with_pip=False)
    site = next((runtime / "venv" / "lib").glob("python*/site-packages"))
    source_site = next((ROOT / ".venv" / "lib").glob("python*/site-packages"))
    (site / "project.pth").write_text(str(source_site) + "\n")
    port = 18998
    process = subprocess.Popen(
        [str(runtime / "venv" / "bin" / "python"), "-m", "uvicorn", "proxy.agents.app:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning", "--ws", "none"],
        cwd=str(runtime / "app"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "MODELMATCH_LOG_DIR": str(runtime / "logs"),
             "MODELMATCH_AGENTS_STATE_FILE": str(runtime / "state" / "s.json")})
    try:
        import time
        import urllib.request
        for _ in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:{}/health".format(port), timeout=1) as response:
                    assert json.loads(response.read())["service"] == "modelmatch-agents-proxy"
                    return
            except OSError:
                time.sleep(0.2)
        pytest.fail("the proxy did not start from the service's own copy")
    finally:
        process.terminate()
        process.wait(timeout=5)
