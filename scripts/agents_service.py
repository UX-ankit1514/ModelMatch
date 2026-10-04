#!/usr/bin/env python3
"""Keep the agents proxy running in the background on macOS (a per-user LaunchAgent).

  agents_service.py install | uninstall | restart | sync | status

Why: once Codex's model routing is on, every Codex surface (CLI, IDE extension, the
Codex app) sends its model requests to the proxy, and some of them may start before
any hook has had a chance to start it. The LaunchAgent starts the proxy at login and
restarts it if it stops. It runs only the proxy, as you.

Why it runs from its own copy: macOS refuses to let background services read files in
Desktop, Documents and Downloads ("Operation not permitted"), and this folder is often
there. So the service gets, in ~/Library/Application Support/ModelMatch/agents:
  venv/    its own Python packages        app/proxy/   a copy of the proxy code
  app/.env a copy of your settings        logs/ state/ its logs and session state
`install` and `restart` refresh that copy from this folder, so after changing .env or
updating ModelMatch, run `restart` (or the installer again).

Set MODELMATCH_NO_LAUNCHD=1 to write the files without asking launchd (tests).
Set MODELMATCH_RUNTIME_DIR to put the copy somewhere else (tests).
"""

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))

import agents_common as common  # noqa: E402

MARKER = ".modelmatch-runtime"
IMPORT_CHECK = "import fastapi, uvicorn, httpx, zstandard"


def runtime_dir():
    custom = os.environ.get("MODELMATCH_RUNTIME_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / "Library" / "Application Support" / "ModelMatch" / "agents"


def runtime_python(runtime):
    return runtime / "venv" / "bin" / "python"


def collect(base):
    """(relative path, absolute path) of the proxy code and .env found under `base`, sorted."""
    files = []
    for path in sorted((base / "proxy").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files.append((path.relative_to(base), path))
    if (base / ".env").exists():
        files.append((Path(".env"), base / ".env"))
    return files


def digest(items):
    h = hashlib.sha256()
    for relative, path in items:
        h.update(str(relative).encode("utf-8"))
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"?")
    return h.hexdigest()


def copy_is_current(runtime):
    source, copy = collect(REPO), collect(runtime / "app")
    return [r for r, _ in source] == [r for r, _ in copy] and digest(source) == digest(copy)


def sync_app(runtime):
    """Refresh the service's copy of the proxy code and settings. Returns True if anything changed."""
    if copy_is_current(runtime):
        return False
    app = runtime / "app"
    app.mkdir(parents=True, exist_ok=True)
    (runtime / MARKER).write_text("ModelMatch background service files (safe to delete)\n")
    shutil.rmtree(str(app / "proxy"), ignore_errors=True)
    shutil.copytree(str(REPO / "proxy"), str(app / "proxy"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if (REPO / ".env").exists():
        shutil.copyfile(str(REPO / ".env"), str(app / ".env"))
        os.chmod(str(app / ".env"), 0o600)  # it can hold your router key
    else:
        try:
            (app / ".env").unlink()
        except OSError:
            pass
    return True


def build_venv(runtime):
    """Give the service its own Python packages (they can't live in Desktop). Returns an error text or None."""
    python = runtime_python(runtime)
    if not python.exists():
        result = subprocess.run([sys.executable, "-m", "venv", str(runtime / "venv")], capture_output=True, text=True)
        if result.returncode != 0:
            return "could not create the service's Python environment: " + result.stderr.strip()[-300:]
    if subprocess.run([str(python), "-c", IMPORT_CHECK], capture_output=True).returncode == 0:
        return None
    pip = [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    subprocess.run(pip + ["--upgrade", "pip"], capture_output=True)
    result = subprocess.run(pip + ["-r", str(REPO / "requirements.txt"), "zstandard>=0.22"], capture_output=True,
                            text=True)
    if result.returncode != 0:
        return "package install for the service failed (is the internet on?): " + result.stderr.strip()[-300:]
    return None


def plist_contents(cfg, runtime):
    logs = runtime / "logs"
    return {
        "Label": common.LAUNCHD_LABEL,
        "ProgramArguments": [str(runtime_python(runtime)), "-m", "uvicorn", "proxy.agents.app:create_app", "--factory",
                             "--host", cfg.host, "--port", str(cfg.port), "--log-level", "warning", "--ws", "none"],
        "WorkingDirectory": str(runtime / "app"),
        "EnvironmentVariables": {
            "MODELMATCH_LOG_DIR": str(logs),
            "MODELMATCH_AGENTS_STATE_FILE": str(runtime / "state" / "agents-sessions.json"),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "agents-proxy.out.log"),
        "StandardErrorPath": str(logs / "agents-proxy.out.log"),
    }


def launchctl(*args):
    try:
        result = subprocess.run(["launchctl"] + list(args), capture_output=True, text=True, timeout=15)
        return result.returncode, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def domain():
    return "gui/{}".format(os.getuid())


def wait_healthy(cfg, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if common.proxy_health(cfg, timeout=0.5):
            return True
        time.sleep(0.4)
    return False


def tail(path, lines=6):
    try:
        return "\n".join("     " + line for line in path.read_text(errors="replace").strip().splitlines()[-lines:])
    except OSError:
        return "     (no log yet)"


def install(cfg, log):
    runtime = runtime_dir()
    (runtime / "logs").mkdir(parents=True, exist_ok=True)
    (runtime / "state").mkdir(parents=True, exist_ok=True)
    sync_app(runtime)
    if cfg.use_launchd:
        print("  preparing the service's own copy in {} ...".format(runtime))
        problem = build_venv(runtime)
        if problem:
            print("  " + problem)
            return 1
    path = cfg.launchd_plist
    path.parent.mkdir(parents=True, exist_ok=True)
    data = plistlib.dumps(plist_contents(cfg, runtime))
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
        print("  wrote {}".format(path))
    if not cfg.use_launchd:
        print("  (launchd skipped)")
        return 0
    common.stop_proxy(cfg, log)  # a proxy started by a hook would hold the port
    launchctl("bootout", common.launchd_target())
    code, out = launchctl("bootstrap", domain(), str(path))
    if code != 0:
        print("  launchd could not load it: {}".format(out))
        return 1
    if not wait_healthy(cfg, 20):
        print("  the background service loaded but the proxy did not start. Its log says:")
        print(tail(runtime / "logs" / "agents-proxy.out.log"))
        return 1
    print("  background service loaded and answering ({})".format(common.LAUNCHD_LABEL))
    return 0


def uninstall(cfg, log):
    path = cfg.launchd_plist
    if cfg.use_launchd:
        launchctl("bootout", common.launchd_target())
    if path.exists():
        path.unlink()
        print("  removed {}".format(path))
    else:
        print("  no background service installed")
    runtime = runtime_dir()
    if (runtime / MARKER).exists():
        shutil.rmtree(str(runtime), ignore_errors=True)
        print("  removed the service's files in {}".format(runtime))
    return 0


def sync(cfg, log):
    changed = sync_app(runtime_dir())
    print("  service copy refreshed" if changed else "  service copy already up to date")
    return 0


def restart(cfg, log):
    if cfg.use_launchd and cfg.launchd_plist.exists():
        sync_app(runtime_dir())
        code, out = launchctl("kickstart", "-k", common.launchd_target())
        print("  background service restarted" if code == 0 else "  restart failed: {}".format(out))
        return 0 if code == 0 else 1
    common.stop_proxy(cfg, log)
    return 0 if common.ensure_proxy(cfg, log, wait_seconds=15) else 1


def status(cfg, log):
    runtime = runtime_dir()
    loaded = None
    if cfg.use_launchd:
        loaded = launchctl("print", common.launchd_target())[0] == 0
    current = copy_is_current(runtime)
    print(json.dumps({"plist": str(cfg.launchd_plist), "installed": cfg.launchd_plist.exists(), "loaded": loaded,
                      "runtime_dir": str(runtime), "runtime_current": current}))
    return 0


def main(argv):
    commands = {"install": install, "uninstall": uninstall, "restart": restart, "sync": sync, "status": status}
    if len(argv) != 2 or argv[1] not in commands:
        print(__doc__)
        return 2
    cfg = common.AgentConfig("codex")
    log = common.setup_logging(cfg)
    return commands[argv[1]](cfg, log)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
