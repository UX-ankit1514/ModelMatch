#!/usr/bin/env python3
"""ModelMatch hook for Claude Code.

Handles two hook events (dispatching on "hook_event_name" from stdin):
  SessionStart      make sure the local proxy is running and register the session. No routing.
  UserPromptSubmit  get ONE model recommendation, ask Use / Keep, save the choice.

Golden rule: this hook must never make Claude Code unusable. Every path exits
with code 0, and stdout only ever carries one small JSON object with a
"systemMessage" for the user. (Plain stdout on these two events would be
injected into Claude's context, so nothing else is printed.)

Also usable from the command line (used by the scripts in ../scripts):
  python3 hooks/modelmatch_hook.py --start-proxy | --stop-proxy | --status

Standard library only, Python 3.7+.
"""

import hashlib
import json
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SERVICE_NAME = "modelmatch-proxy"
HOOK_BUDGET_SECONDS = 100  # stays under the 120s timeout set in .claude/settings.json
CONFIRM_UIS = ("auto", "tty", "dialog", "auto-accept", "never")

log = logging.getLogger("modelmatch.hook")


class HookDeadline(Exception):
    pass


class ProxyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _read_dotenv(path):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


class Config:
    def __init__(self, env=None):
        merged = _read_dotenv(ROOT / ".env")
        merged.update(os.environ if env is None else env)

        def get(name, default=""):
            value = merged.get(name)
            return default if value is None or str(value).strip() == "" else str(value).strip()

        def number(name, default):
            try:
                return float(get(name, str(default)))
            except ValueError:
                return float(default)

        self.host = get("MODELMATCH_HOST", "127.0.0.1")
        self.port = int(number("MODELMATCH_PORT", 8787))
        self.proxy_url = "http://{}:{}".format(self.host, self.port)
        ui = get("MODELMATCH_CONFIRM_UI", "auto").lower()
        self.confirm_ui = ui if ui in CONFIRM_UIS else "auto"
        self.confirm_timeout = max(1.0, number("MODELMATCH_CONFIRM_TIMEOUT", 30))
        self.recommend_timeout = max(2.0, number("MODELMATCH_API_TIMEOUT", 25) + 5)
        self.disabled = get("MODELMATCH_DISABLED", "0").lower() in ("1", "true", "yes", "on")
        self.log_prompts = get("MODELMATCH_LOG_PROMPTS", "0").lower() in ("1", "true", "yes", "on")
        self.log_dir = Path(get("MODELMATCH_LOG_DIR", str(ROOT / "logs")))
        self.state_dir = Path(get("MODELMATCH_STATE_DIR", str(ROOT / "state")))
        self.venv_python = Path(get("MODELMATCH_PYTHON", str(ROOT / ".venv" / "bin" / "python")))


def setup_logging(cfg):
    log.setLevel(logging.INFO)
    log.propagate = False
    if log.handlers:
        return
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(cfg.log_dir / "hook.log"), maxBytes=1000000, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | hook  | %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())


def short(session_id):
    return (session_id or "-")[:8]


def fingerprint(prompt):
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Talking to the local proxy
# ---------------------------------------------------------------------------

# Never send local proxy traffic through an HTTP proxy from the environment.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(method, url, payload=None, timeout=5.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status, _json_or_none(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json_or_none(exc.read())
    except urllib.error.URLError as exc:
        reason = exc.reason
        raise ProxyError("timed out" if "timed out" in str(reason) else "not reachable ({})".format(reason))
    except OSError as exc:  # socket.timeout and friends
        raise ProxyError("timed out" if "timed out" in str(exc) else "connection error ({})".format(exc))


def _json_or_none(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def proxy_health(cfg, timeout=0.8):
    try:
        status, data = http_json("GET", cfg.proxy_url + "/health", timeout=timeout)
    except ProxyError:
        return None
    if status == 200 and isinstance(data, dict) and data.get("service") == SERVICE_NAME:
        return data
    return None


def start_proxy(cfg):
    """Launch the proxy in the background (detached from Claude Code)."""
    if not cfg.venv_python.exists():
        log.error("proxy start failed | python environment missing at %s (run scripts/install.sh)", cfg.venv_python)
        return False
    try:
        import fcntl
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        lock = open(str(cfg.state_dir / "proxy.start.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("proxy start already in progress (another session) | waiting")
            lock.close()
            return True
        out = open(str(cfg.log_dir / "proxy.out.log"), "a")
        process = subprocess.Popen(
            [str(cfg.venv_python), "-m", "uvicorn", "proxy.app:create_app", "--factory",
             "--host", cfg.host, "--port", str(cfg.port), "--log-level", "warning"],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
        out.close()
        (cfg.state_dir / "proxy.pid").write_text(str(process.pid))
        log.info("proxy starting | pid=%s | %s", process.pid, cfg.proxy_url)
        lock.close()  # releases the flock; the bound port now guards against duplicates
        return True
    except OSError as exc:
        log.error("proxy start failed | %s", exc)
        return False


def ensure_proxy(cfg, wait_seconds):
    if proxy_health(cfg):
        return True
    log.info("proxy health: not running | starting it")
    if not start_proxy(cfg):
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if proxy_health(cfg):
            log.info("proxy health: ok")
            return True
    log.error("proxy health: did not come up within %.0fs (see logs/proxy.out.log)", wait_seconds)
    return False


def stop_proxy(cfg):
    health = proxy_health(cfg)
    pid = health.get("pid") if health else None
    if pid is None:
        try:
            pid = int((cfg.state_dir / "proxy.pid").read_text().strip())
        except (OSError, ValueError):
            return False
        # Only stop a process that really is our proxy.
        try:
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                                     timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            command = ""
        if "proxy.app" not in command:
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
        (cfg.state_dir / "proxy.pid").unlink()
    except OSError:
        pass
    log.info("proxy stopped | pid=%s", pid)
    return True


def routing_enabled(cfg):
    """True when this Claude Code session sends its model requests through our proxy."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if not base:
        return False
    parsed = urlparse(base)
    return parsed.hostname in ("127.0.0.1", "localhost") and (parsed.port or 80) == cfg.port


# ---------------------------------------------------------------------------
# "Use recommended model?" confirmation
# ---------------------------------------------------------------------------


def confirm(cfg, rec):
    """Return (decision, method, detail); decision is accepted | rejected | timeout | unavailable."""
    if cfg.confirm_ui == "auto-accept":
        return "accepted", "auto-accept", ""
    if cfg.confirm_ui == "never":
        return "rejected", "never", ""
    methods = {"tty": ["tty"], "dialog": ["dialog"]}.get(cfg.confirm_ui, ["tty", "dialog"])
    problems = []
    for method in methods:
        ask = _confirm_tty if method == "tty" else _confirm_dialog
        decision, detail = ask(rec, cfg.confirm_timeout)
        if decision != "unavailable":
            return decision, method, detail
        problems.append(detail)
    return "unavailable", "none", "; ".join(problems)


def _confirm_tty(rec, timeout):
    try:
        fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError as exc:
        return "unavailable", "no terminal ({})".format(exc.strerror or exc)
    import termios
    import tty

    old = None
    decision = "timeout"
    try:
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            old = None
        lines = ["", "  ModelMatch", "  Recommended model: " + rec["recommended_model"]]
        if rec.get("recommendation_reason"):
            lines.append("  Reason: " + rec["recommendation_reason"])
        lines += ["", "  Use recommended model? [Y/n] "]
        os.write(fd, "\r\n".join(lines).encode("utf-8"))
        if old is not None:
            tty.setcbreak(fd)
            termios.tcflush(fd, termios.TCIFLUSH)  # ignore keys typed before the question
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            key = os.read(fd, 1)
            if not key:
                break
            if key in (b"y", b"Y", b"\r", b"\n"):
                decision = "accepted"
                break
            if key in (b"n", b"N", b"\x1b", b"\x03", b"\x04"):
                decision = "rejected"
                break
        answer = {"accepted": "yes", "rejected": "no, keeping current model"}.get(
            decision, "no answer, keeping current model")
        os.write(fd, (answer + "\r\n").encode("utf-8"))
        return decision, "terminal"
    except OSError as exc:
        return "unavailable", "terminal error ({})".format(exc)
    finally:
        if old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old)  # never wait on the terminal
            except termios.error:
                pass
        os.close(fd)


_DIALOG_SCRIPT = [
    "on run argv",
    "set theMessage to item 1 of argv",
    "set useLabel to item 2 of argv",
    "set waitSeconds to (item 3 of argv) as integer",
    'display dialog theMessage with title "ModelMatch" buttons {"Keep current", useLabel} '
    'default button useLabel cancel button "Keep current" giving up after waitSeconds with icon note',
    "set answer to result",
    'if gave up of answer then return "TIMEOUT"',
    'return "BUTTON:" & (button returned of answer)',
    "end run",
]


def _confirm_dialog(rec, timeout):
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return "unavailable", "macOS popup not available"
    display = rec["recommended_model"]
    use_label = "Use " + (display if len(display) <= 32 else display[:31] + "…")
    message = "Recommended model: " + display
    if rec.get("recommendation_reason"):
        message += "\n\nReason: " + rec["recommendation_reason"]
    if rec.get("substituted_from"):
        message += "\n\n(ModelMatch named " + rec["substituted_from"] + "; this is its current equivalent.)"
    message += "\n\nUse this model for this prompt?"

    command = ["osascript"]
    for line in _DIALOG_SCRIPT:
        command += ["-e", line]
    command += ["--", message, use_label, str(int(timeout))]
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, universal_newlines=True)
        out, err = process.communicate(timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        return "timeout", "popup"
    except OSError as exc:
        return "unavailable", "popup failed to open ({})".format(exc)
    finally:
        if process is not None and process.poll() is None:
            process.kill()  # never leave a popup behind
    out = (out or "").strip()
    if process.returncode != 0:
        if "-128" in (err or ""):  # "Keep current" is the cancel button
            return "rejected", "popup"
        return "unavailable", "popup failed ({})".format(" ".join((err or "").split())[:120])
    if out == "TIMEOUT":
        return "timeout", "popup"
    if out.startswith("BUTTON:"):
        return ("accepted" if out[len("BUTTON:"):] == use_label else "rejected"), "popup"
    return "unavailable", "popup gave an unexpected answer"


# ---------------------------------------------------------------------------
# Event handlers (each returns an optional message for the user)
# ---------------------------------------------------------------------------


def handle_session_start(cfg, data):
    session_id = data.get("session_id") or ""
    source = data.get("source") or "-"
    model = data.get("model") or None
    routing = routing_enabled(cfg)
    log.info("hook received | SessionStart | session=%s | source=%s | model=%s | routing=%s",
             short(session_id), source, model or "-", "on" if routing else "off")

    if not ensure_proxy(cfg, wait_seconds=8):
        log.warning("fallback triggered | proxy unavailable at session start")
        message = "ModelMatch: the local proxy couldn't start, so there will be no model recommendations."
        if routing:
            message += (" Model routing is switched on for this project, so Claude may not reach the API. "
                        "Run: bash \"{}/scripts/doctor.sh\"".format(ROOT))
        return message

    try:
        status, _ = http_json("POST", cfg.proxy_url + "/session/start",
                              {"session_id": session_id, "cwd": data.get("cwd"), "source": source, "model": model},
                              timeout=3)
        if status != 200:
            log.warning("session registration failed | HTTP %s", status)
    except ProxyError as exc:
        log.warning("session registration failed | %s", exc)

    if source != "startup":
        return None
    return "ModelMatch is on ({}).".format(
        "model routing active" if routing else "recommendations only, routing off")


def handle_user_prompt(cfg, data):
    session_id = data.get("session_id") or "unknown"
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        log.warning("hook received | UserPromptSubmit without a prompt | session=%s", short(session_id))
        return None
    log.info("hook received | UserPromptSubmit | session=%s | prompt=%d chars fp=%s | cwd=%s",
             short(session_id), len(prompt), fingerprint(prompt), data.get("cwd") or "-")
    if cfg.log_prompts:
        log.info("prompt preview | session=%s | %r", short(session_id), prompt[:80])
    if not prompt.strip():
        return None
    if prompt.lstrip().startswith("/"):
        log.info("skipped | slash command")
        return None

    if not ensure_proxy(cfg, wait_seconds=5):
        log.warning("fallback triggered | proxy unavailable | keeping current model")
        return "ModelMatch is unavailable right now (local proxy not running). Continuing with your current model."

    log.info("recommendation request | session=%s", short(session_id))
    try:
        status, rec = http_json("POST", cfg.proxy_url + "/recommend",
                                {"session_id": session_id, "prompt": prompt, "prompt_id": data.get("prompt_id"),
                                 "cwd": data.get("cwd")},
                                timeout=cfg.recommend_timeout)
    except ProxyError as exc:
        log.warning("fallback triggered | recommendation failed: %s | keeping current model", exc)
        return "ModelMatch couldn't get a recommendation ({}). Continuing with your current model.".format(exc)
    if status != 200 or not isinstance(rec, dict) or not rec.get("ok") or not rec.get("recommended_model"):
        error = rec.get("error") if isinstance(rec, dict) else None
        log.warning("fallback triggered | recommendation failed: HTTP %s %s | keeping current model", status, error or "")
        return "ModelMatch couldn't get a recommendation ({}). Continuing with your current model.".format(
            error or "HTTP {}".format(status))

    name = rec["recommended_model"]
    reason = rec.get("recommendation_reason") or ""
    reason_text = " " + reason if reason else ""
    substituted = (" (ModelMatch named {}; using its current equivalent.)".format(rec["substituted_from"])
                   if rec.get("substituted_from") else "")
    log.info("recommended model | session=%s | %s | routable=%s | same_as_current=%s",
             short(session_id), name, rec.get("routable"), rec.get("same_as_current"))

    def save(accepted, decided_by):
        try:
            code, result = http_json("POST", cfg.proxy_url + "/selection",
                                     {"session_id": session_id, "recommendation_id": rec.get("recommendation_id"),
                                      "accepted": accepted, "decided_by": decided_by}, timeout=3)
            return result if code == 200 and isinstance(result, dict) else {}
        except ProxyError as exc:
            log.warning("could not save selection | %s", exc)
            return {}

    if not rec.get("routable"):
        save(False, "unsupported")
        log.info("not applied | %s is not routable: %s", name, rec.get("routing_note"))
        return "ModelMatch recommends {}.{} {} Keeping your current model.".format(
            name, reason_text, rec.get("routing_note") or "Automatic routing isn't set up for it.")
    if rec.get("same_as_current"):
        save(False, "already-current")
        return "ModelMatch recommends {}, which is already your current model.".format(name)
    if not routing_enabled(cfg):
        save(False, "routing-off")
        log.info("not applied | routing is off for this session (ANTHROPIC_BASE_URL not pointing at the proxy)")
        return "ModelMatch recommends {}.{}{} (Recommendation only: model routing is off for this session.)".format(
            name, reason_text, substituted)

    decision, method, detail = confirm(cfg, rec)
    accepted = decision == "accepted"
    result = save(accepted, method if decision in ("accepted", "rejected") else decision)
    log.info("user %s | session=%s | via %s%s", decision, short(session_id), method,
             " | " + detail if detail else "")

    if accepted and result.get("applied"):
        return "ModelMatch: using {} for this prompt.{}{}".format(name, reason_text, substituted)
    if accepted:
        log.warning("fallback triggered | accepted but the proxy did not apply the route | keeping current model")
        return "ModelMatch couldn't apply {}. Keeping your current model.".format(name)
    if decision == "rejected":
        return "ModelMatch: kept your current model (recommended {}).".format(name)
    if decision == "timeout":
        return "ModelMatch: no answer within {:.0f}s, kept your current model (recommended {}).".format(
            cfg.confirm_timeout, name)
    log.warning("fallback triggered | confirmation UI unavailable: %s | keeping current model", detail)
    return ("ModelMatch recommends {}.{} The Use/Keep question couldn't be shown, so your current model "
            "was kept.".format(name, reason_text))


def claim(cfg, event, data):
    """True if this invocation should do the work.

    The same hook can be configured twice (user-level and project-level settings).
    Only the first invocation per prompt/session start acts; the other exits quietly,
    so you never see two popups for one prompt.
    """
    session = data.get("session_id") or ""
    if event == "UserPromptSubmit":
        unique = data.get("prompt_id") or "{}-{}".format(fingerprint(data.get("prompt") or ""), int(time.time() // 10))
    else:
        unique = "{}-{}".format(data.get("source") or "", int(time.time() // 10))
    name = hashlib.sha256("|".join([event, session, str(unique)]).encode("utf-8")).hexdigest()[:24]
    folder = cfg.state_dir / "handled"
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


def is_headless():
    """Scripted runs (claude -p, SDK) have nobody to click a popup."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_hook(cfg):
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        log.error("hook received malformed JSON on stdin (%d bytes) | ignoring", len(raw))
        return None
    if not isinstance(data, dict):
        return None
    event = data.get("hook_event_name")
    if cfg.disabled:
        # Paused: no recommendations. But if this session's API traffic goes through
        # the proxy, it must still be running or Claude couldn't reach the API.
        if event in ("SessionStart", "UserPromptSubmit") and routing_enabled(cfg):
            ensure_proxy(cfg, wait_seconds=8 if event == "SessionStart" else 5)
        log.info("paused via MODELMATCH_DISABLED | %s ignored", event)
        return None
    if event in ("SessionStart", "UserPromptSubmit") and not claim(cfg, event, data):
        log.info("skipped | %s already handled by another copy of this hook", event)
        return None
    if event == "SessionStart":
        return handle_session_start(cfg, data)
    if event == "UserPromptSubmit":
        if is_headless() and cfg.confirm_ui != "auto-accept":
            log.info("skipped | non-interactive run (no one to ask)")
            return None
        return handle_user_prompt(cfg, data)
    log.info("ignored event %s", event)
    return None


def _on_deadline(signum, frame):
    raise HookDeadline()


def main(argv):
    cfg = Config()
    setup_logging(cfg)

    if len(argv) > 1:  # command-line helpers for the scripts
        command = argv[1]
        if command == "--start-proxy":
            ok = ensure_proxy(cfg, wait_seconds=15)
            print("Proxy is running at " + cfg.proxy_url if ok else "Proxy did not start. See logs/proxy.out.log")
            return 0 if ok else 1
        if command == "--stop-proxy":
            stopped = stop_proxy(cfg)
            print("Proxy stopped." if stopped else "Proxy was not running.")
            return 0
        if command == "--status":
            health = proxy_health(cfg, timeout=2)
            print(json.dumps(health) if health else "not running")
            return 0 if health else 1
        print("usage: modelmatch_hook.py [--start-proxy | --stop-proxy | --status]  (no args = hook mode)")
        return 2

    message = None
    try:
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, _on_deadline)
            signal.alarm(HOOK_BUDGET_SECONDS)
        message = run_hook(cfg)
    except HookDeadline:
        log.error("fallback triggered | hook ran out of time | keeping current model")
        message = "ModelMatch took too long, so your current model was kept."
    except BaseException:  # fail open on anything, including KeyboardInterrupt
        log.exception("fallback triggered | unexpected hook error | keeping current model")
        message = None
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    if message:
        try:
            sys.stdout.write(json.dumps({"systemMessage": message}))
            sys.stdout.flush()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv)
    except BaseException:
        code = 0
    sys.exit(code)
