"""Shared parts of the Codex and Copilot hooks.

Both talk to the agents proxy (proxy/agents/app.py, default 127.0.0.1:8788), the
sibling of the Claude Code proxy that knows Codex's and Copilot's model lists.
The .env parsing, HTTP helper and Use / Keep confirmation (macOS popup, terminal
fallback) are the Claude Code hook's own, imported unchanged from
modelmatch_hook.py, so all three tools ask the same question the same way.

Standard library only, Python 3.7+.
"""

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import modelmatch_hook as base  # noqa: E402  (the Claude Code hook; reused, never modified)

ROOT = base.ROOT
SERVICE_NAME = "modelmatch-agents-proxy"
LAUNCHD_LABEL = "com.modelmatch.agents-proxy"
HOOK_BUDGET_SECONDS = 100  # stays under the 120 s timeout the installers configure
ProxyError = base.ProxyError
HookDeadline = base.HookDeadline
fingerprint = base.fingerprint
short = base.short
http_json = base.http_json


# ---------------------------------------------------------------------------
# Configuration and logging
# ---------------------------------------------------------------------------


class AgentConfig:
    """Settings for one tool's hook ("codex" or "copilot"), from .env and the environment."""

    def __init__(self, tool, env=None):
        merged = base._read_dotenv(ROOT / ".env")
        merged.update(os.environ if env is None else env)

        def get(name, default=""):
            value = merged.get(name)
            return default if value is None or str(value).strip() == "" else str(value).strip()

        def number(name, default):
            try:
                return float(get(name, str(default)))
            except ValueError:
                return float(default)

        def flag(name):
            return get(name, "0").lower() in ("1", "true", "yes", "on")

        self.tool = tool
        self.host = get("MODELMATCH_HOST", "127.0.0.1")
        self.port = int(number("MODELMATCH_AGENTS_PORT", 8788))
        self.proxy_url = "http://{}:{}".format(self.host, self.port)
        ui = get("MODELMATCH_CONFIRM_UI", "auto").lower()
        self.confirm_ui = ui if ui in base.CONFIRM_UIS else "auto"
        self.confirm_timeout = max(1.0, number("MODELMATCH_CONFIRM_TIMEOUT", 30))
        self.recommend_timeout = max(2.0, number("MODELMATCH_API_TIMEOUT", 25) + 5)
        self.disabled = flag("MODELMATCH_DISABLED") or flag("MODELMATCH_{}_DISABLED".format(tool.upper()))
        self.log_prompts = flag("MODELMATCH_LOG_PROMPTS")
        self.notify = get("MODELMATCH_NOTIFY", "1").lower() not in ("0", "false", "no", "off")
        self.log_dir = Path(get("MODELMATCH_LOG_DIR", str(ROOT / "logs")))
        self.state_dir = Path(get("MODELMATCH_STATE_DIR", str(ROOT / "state")))
        self.venv_python = Path(get("MODELMATCH_PYTHON", str(ROOT / ".venv" / "bin" / "python")))
        self.codex_home = Path(get("MODELMATCH_CODEX_HOME",
                                   get("CODEX_HOME", str(Path.home() / ".codex")))).expanduser()
        self.launchd_plist = Path(get("MODELMATCH_LAUNCHD_PLIST", str(
            Path.home() / "Library" / "LaunchAgents" / (LAUNCHD_LABEL + ".plist"))))
        self.use_launchd = sys.platform == "darwin" and not flag("MODELMATCH_NO_LAUNCHD")


def setup_logging(cfg):
    log = logging.getLogger("modelmatch." + cfg.tool)
    log.setLevel(logging.INFO)
    log.propagate = False
    if log.handlers:
        return log
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(cfg.log_dir / "{}-hook.log".format(cfg.tool)), maxBytes=1000000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | {:<7} | %(message)s".format(cfg.tool),
                                               "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())
    return log


# ---------------------------------------------------------------------------
# The agents proxy
# ---------------------------------------------------------------------------


def proxy_command(cfg):
    return [str(cfg.venv_python), "-m", "uvicorn", "proxy.agents.app:create_app", "--factory",
            "--host", cfg.host, "--port", str(cfg.port), "--log-level", "warning", "--ws", "none"]


def proxy_health(cfg, timeout=0.8):
    try:
        status, data = http_json("GET", cfg.proxy_url + "/health", timeout=timeout)
    except ProxyError:
        return None
    if status == 200 and isinstance(data, dict) and data.get("service") == SERVICE_NAME:
        return data
    return None


def _launchctl(*args):
    try:
        return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True, timeout=10).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def launchd_target():
    return "gui/{}/{}".format(os.getuid(), LAUNCHD_LABEL)


def start_proxy(cfg, log):
    """Start the agents proxy in the background (or ask launchd to, when its LaunchAgent is installed)."""
    if cfg.use_launchd and cfg.launchd_plist.exists():
        if _launchctl("kickstart", launchd_target()) == 0:
            log.info("proxy start requested from launchd (%s)", LAUNCHD_LABEL)
            return True
        log.warning("launchd could not start %s | starting the proxy directly", LAUNCHD_LABEL)
    if not cfg.venv_python.exists():
        log.error("proxy start failed | python environment missing at %s (run the installer)", cfg.venv_python)
        return False
    try:
        import fcntl
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        lock = open(str(cfg.state_dir / "agents-proxy.start.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("proxy start already in progress (another session) | waiting")
            lock.close()
            return True
        out = open(str(cfg.log_dir / "agents-proxy.out.log"), "a")
        process = subprocess.Popen(proxy_command(cfg), cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=out,
                                   stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        out.close()
        (cfg.state_dir / "agents-proxy.pid").write_text(str(process.pid))
        log.info("proxy starting | pid=%s | %s", process.pid, cfg.proxy_url)
        lock.close()
        return True
    except OSError as exc:
        log.error("proxy start failed | %s", exc)
        return False


def ensure_proxy(cfg, log, wait_seconds):
    if proxy_health(cfg):
        return True
    log.info("proxy health: not running | starting it")
    if not start_proxy(cfg, log):
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if proxy_health(cfg):
            log.info("proxy health: ok")
            return True
    log.error("proxy health: did not come up within %.0fs (see logs/agents-proxy.out.log)", wait_seconds)
    return False


def stop_proxy(cfg, log):
    """Stop the agents proxy. (Under launchd it is started again at once: that's a restart.)"""
    health = proxy_health(cfg)
    pid = health.get("pid") if health else None
    if pid is None:
        try:
            pid = int((cfg.state_dir / "agents-proxy.pid").read_text().strip())
        except (OSError, ValueError):
            return False
        try:
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                                     timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            command = ""
        if "proxy.agents.app" not in command:
            return False
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return False
    for _ in range(25):
        time.sleep(0.2)
        if not proxy_health(cfg, timeout=0.3):
            break
    try:
        (cfg.state_dir / "agents-proxy.pid").unlink()
    except OSError:
        pass
    log.info("proxy stopped | pid=%s", pid)
    return True


# ---------------------------------------------------------------------------
# Recommendation, confirmation, selection
# ---------------------------------------------------------------------------


def get_recommendation(cfg, log, payload):
    """(recommendation dict, None) or (None, short reason it failed)."""
    log.info("recommendation request | session=%s", short(payload.get("session_id")))
    try:
        status, rec = http_json("POST", cfg.proxy_url + "/recommend", payload, timeout=cfg.recommend_timeout)
    except ProxyError as exc:
        log.warning("fallback triggered | recommendation failed: %s | keeping current model", exc)
        return None, str(exc)
    if status != 200 or not isinstance(rec, dict) or not rec.get("ok") or not rec.get("recommended_model"):
        error = rec.get("error") if isinstance(rec, dict) else None
        log.warning("fallback triggered | recommendation failed: HTTP %s %s | keeping current model", status, error or "")
        return None, error or "HTTP {}".format(status)
    log.info("recommended model | session=%s | %s | routable=%s | same_as_current=%s", short(payload.get("session_id")),
             rec["recommended_model"], rec.get("routable"), rec.get("same_as_current"))
    return rec, None


def save_selection(cfg, log, client, session_id, rec, accepted, decided_by):
    try:
        code, result = http_json("POST", cfg.proxy_url + "/selection",
                                 {"client": client, "session_id": session_id,
                                  "recommendation_id": rec.get("recommendation_id"), "accepted": accepted,
                                  "decided_by": decided_by}, timeout=3)
        return result if code == 200 and isinstance(result, dict) else {}
    except ProxyError as exc:
        log.warning("could not save selection | %s", exc)
        return {}


def confirm(cfg, rec):
    """(decision, method, detail): the Claude Code hook's Use / Keep question, unchanged."""
    return base.confirm(cfg, rec)


def register_session(cfg, log, client, session_id, cwd, source, model):
    try:
        status, _ = http_json("POST", cfg.proxy_url + "/session/start",
                              {"client": client, "session_id": session_id, "cwd": cwd, "source": source,
                               "model": model}, timeout=3)
        if status != 200:
            log.warning("session registration failed | HTTP %s", status)
    except ProxyError as exc:
        log.warning("session registration failed | %s", exc)


def claim(cfg, event, session_id, unique):
    """True if this invocation should do the work (the same hook may be configured twice)."""
    name = hashlib.sha256("|".join([cfg.tool, event, session_id or "", str(unique)]).encode("utf-8")).hexdigest()[:24]
    folder = cfg.state_dir / ("handled-" + cfg.tool)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.close(os.open(str(folder / name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    except OSError:
        return True  # can't tell, so act
    try:  # tidy markers older than a day
        cutoff = time.time() - 86400
        for old in folder.iterdir():
            if old.stat().st_mtime < cutoff:
                old.unlink()
    except OSError:
        pass
    return True


def ancestor_commands(max_depth=6):
    """Command lines of this process's parents (nearest first), to recognise scripted runs."""
    commands, pid = [], os.getppid()
    for _ in range(max_depth):
        if pid <= 1:
            break
        try:
            out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        match = re.match(r"\s*(\d+)\s+(.*)", out)
        if not match:
            break
        commands.append(match.group(2))
        pid = int(match.group(1))
    return commands


def notify(title, subtitle, message):
    """A macOS notification banner (non-blocking). Best effort."""
    script = ["on run argv", "display notification (item 3 of argv) with title (item 1 of argv) "
              "subtitle (item 2 of argv)", "end run"]
    command = ["osascript"]
    for line in script:
        command += ["-e", line]
    command += ["--", title, subtitle, message]
    try:
        subprocess.run(command, capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Running a hook safely
# ---------------------------------------------------------------------------


def read_stdin_json(log):
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        log.error("hook received malformed JSON on stdin (%d bytes) | ignoring", len(raw))
        return None
    return data if isinstance(data, dict) else None


def run_with_deadline(log, work, timeout_message):
    """Run work(); never raise. Returns its result, or timeout_message if it took too long."""
    def on_deadline(signum, frame):
        raise HookDeadline()

    try:
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, on_deadline)
            signal.alarm(HOOK_BUDGET_SECONDS)
        return work()
    except HookDeadline:
        log.error("fallback triggered | hook ran out of time | keeping current model")
        return timeout_message
    except BaseException:  # fail open on anything, including KeyboardInterrupt
        log.exception("fallback triggered | unexpected hook error | keeping current model")
        return None
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)


def proxy_cli(cfg, log, argv):
    """--start-proxy / --stop-proxy / --status for the scripts. Returns an exit code, or None if not a CLI call."""
    if len(argv) < 2:
        return None
    command = argv[1]
    if command == "--start-proxy":
        ok = ensure_proxy(cfg, log, wait_seconds=15)
        print("Proxy is running at " + cfg.proxy_url if ok else "Proxy did not start. See logs/agents-proxy.out.log")
        return 0 if ok else 1
    if command == "--stop-proxy":
        print("Proxy stopped." if stop_proxy(cfg, log) else "Proxy was not running.")
        return 0
    if command == "--status":
        health = proxy_health(cfg, timeout=2)
        print(json.dumps(health) if health else "not running")
        return 0 if health else 1
    return None
