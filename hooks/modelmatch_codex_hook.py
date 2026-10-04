#!/usr/bin/env python3
"""ModelMatch hook for Codex CLI.

Handles two Codex hook events (dispatching on "hook_event_name" from stdin):
  SessionStart      make sure the local agents proxy is running and register the session. No routing.
  UserPromptSubmit  get ONE model recommendation, ask Use / Keep, save the choice.

Codex runs it from ~/.codex/hooks.json (or <project>/.codex/hooks.json) once you have
trusted it with /hooks. The switch itself happens in the agents proxy: with model
routing on, Codex sends its model requests there (openai_base_url in
~/.codex/config.toml) and the proxy uses the accepted model for that one turn.

Golden rule: this hook must never make Codex unusable. Every path exits with code 0
(exit code 2 would block the prompt), and stdout only ever carries one small JSON
object with a "systemMessage" for the user. (Plain stdout on these two events would
be added to the model's context, so nothing else is printed.)

Also usable from the command line (used by the scripts in ../scripts):
  python3 hooks/modelmatch_codex_hook.py --start-proxy | --stop-proxy | --status

Standard library only, Python 3.7+.
"""

import json
import re
import sys
import time
from urllib.parse import urlparse

import agents_common as common

CLIENT = "codex"
_CODEX_BINARY = re.compile(r"(^|/)codex$")
# Codex options that take a value, so the value isn't mistaken for the subcommand.
_VALUE_OPTIONS = {"-c", "--config", "-m", "--model", "-p", "--profile", "-C", "--cd", "-s", "--sandbox", "-a",
                  "--ask-for-approval", "-i", "--image", "--enable", "--disable", "--remote", "--local-provider",
                  "--add-dir", "--remote-auth-token-env"}


def read_codex_config(cfg):
    sys.path.insert(0, str(common.ROOT))
    from proxy.agents.codex_config import read_config
    return read_config(cfg.codex_home)


def routing_enabled(cfg):
    """True when Codex sends its model requests through our proxy (openai_base_url in config.toml)."""
    try:
        base = read_codex_config(cfg).get("openai_base_url")
    except Exception:
        return False
    if not isinstance(base, str) or not base.strip():
        return False
    parsed = urlparse(base.strip())
    return parsed.hostname in ("127.0.0.1", "localhost") and (parsed.port or 80) == cfg.port


def codex_subcommand(command):
    """The subcommand of a `codex ...` command line ("exec", "resume", ...), or None."""
    words = command.split()
    for start, word in enumerate(words):
        if _CODEX_BINARY.search(word):
            break
    else:
        return None
    skip = False
    for word in words[start + 1:]:
        if skip:
            skip = False
        elif word in _VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            return word
    return None


def is_headless():
    """`codex exec` runs have nobody to click a popup."""
    return any(codex_subcommand(command) in ("exec", "e") for command in common.ancestor_commands())


def handle_session_start(cfg, log, data):
    session_id = data.get("session_id") or ""
    source = data.get("source") or "-"
    model = data.get("model") or None
    routing = routing_enabled(cfg)
    log.info("hook received | SessionStart | session=%s | source=%s | model=%s | routing=%s",
             common.short(session_id), source, model or "-", "on" if routing else "off")

    if not common.ensure_proxy(cfg, log, wait_seconds=8):
        log.warning("fallback triggered | proxy unavailable at session start")
        message = "ModelMatch: the local proxy couldn't start, so there will be no model recommendations."
        if routing:
            message += (" Model routing is on for Codex, so Codex may not reach its models. "
                        "Run: bash \"{}/scripts/doctor_codex.sh\"".format(common.ROOT))
        return message

    common.register_session(cfg, log, CLIENT, session_id, data.get("cwd"), source, model)
    if source != "startup":
        return None
    return "ModelMatch is on ({}).".format(
        "model routing active" if routing else "recommendations only, routing off")


def handle_user_prompt(cfg, log, data):
    session_id = data.get("session_id") or "unknown"
    turn_id = data.get("turn_id") or None
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        log.warning("hook received | UserPromptSubmit without a prompt | session=%s", common.short(session_id))
        return None
    log.info("hook received | UserPromptSubmit | session=%s | turn=%s | model=%s | prompt=%d chars fp=%s | cwd=%s",
             common.short(session_id), common.short(turn_id), data.get("model") or "-", len(prompt),
             common.fingerprint(prompt), data.get("cwd") or "-")
    if cfg.log_prompts:
        log.info("prompt preview | session=%s | %r", common.short(session_id), prompt[:80])
    if not prompt.strip():
        return None
    if prompt.lstrip().startswith("/"):
        log.info("skipped | slash command")
        return None

    if not common.ensure_proxy(cfg, log, wait_seconds=5):
        log.warning("fallback triggered | proxy unavailable | keeping current model")
        return "ModelMatch is unavailable right now (local proxy not running). Continuing with your current model."

    rec, error = common.get_recommendation(cfg, log, {
        "client": CLIENT, "session_id": session_id, "turn_id": turn_id, "prompt": prompt, "cwd": data.get("cwd"),
        "current_model": data.get("model")})
    if rec is None:
        return "ModelMatch couldn't get a recommendation ({}). Continuing with your current model.".format(error)

    name = rec["recommended_model"]
    reason = rec.get("recommendation_reason") or ""
    reason_text = " " + reason if reason else ""
    substituted = (" (ModelMatch named {}; this is the closest model in your Codex list.)".format(
        rec["substituted_from"]) if rec.get("substituted_from") else "")

    def save(accepted, decided_by):
        return common.save_selection(cfg, log, CLIENT, session_id, rec, accepted, decided_by)

    if not rec.get("routable"):
        save(False, "unsupported")
        log.info("not applied | %s is not routable: %s", name, rec.get("routing_note"))
        return "ModelMatch recommends {}.{} {} Keeping your current model.".format(
            name, reason_text, rec.get("routing_note") or "Codex can't switch to it.")
    if rec.get("same_as_current"):
        save(False, "already-current")
        return "ModelMatch recommends {}, which is already your current model.".format(name)
    if not routing_enabled(cfg):
        save(False, "routing-off")
        log.info("not applied | routing is off (openai_base_url in config.toml doesn't point at the proxy)")
        return "ModelMatch recommends {}.{}{} (Recommendation only: model routing is off for Codex.)".format(
            name, reason_text, substituted)

    decision, method, detail = common.confirm(cfg, rec)
    accepted = decision == "accepted"
    result = save(accepted, method if decision in ("accepted", "rejected") else decision)
    log.info("user %s | session=%s | via %s%s", decision, common.short(session_id), method,
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


def run_hook(cfg, log):
    data = common.read_stdin_json(log)
    if data is None:
        return None
    event = data.get("hook_event_name")
    session_id = data.get("session_id") or ""
    if cfg.disabled:
        # Paused: no recommendations. But if Codex's model requests go through the
        # proxy, it must still be running or Codex couldn't reach its models.
        if event in ("SessionStart", "UserPromptSubmit") and routing_enabled(cfg):
            common.ensure_proxy(cfg, log, wait_seconds=8 if event == "SessionStart" else 5)
        log.info("paused via MODELMATCH_DISABLED | %s ignored", event)
        return None
    if event == "UserPromptSubmit":
        unique = data.get("turn_id") or "{}-{}".format(common.fingerprint(data.get("prompt") or ""),
                                                       int(time.time() // 10))
    else:
        unique = "{}-{}".format(data.get("source") or "", int(time.time() // 10))
    if event in ("SessionStart", "UserPromptSubmit") and not common.claim(cfg, event, session_id, unique):
        log.info("skipped | %s already handled by another copy of this hook", event)
        return None
    if event == "SessionStart":
        return handle_session_start(cfg, log, data)
    if event == "UserPromptSubmit":
        if cfg.confirm_ui != "auto-accept" and is_headless():
            log.info("skipped | non-interactive run (codex exec: no one to ask)")
            return None
        return handle_user_prompt(cfg, log, data)
    log.info("ignored event %s", event)
    return None


def main(argv):
    cfg = common.AgentConfig(CLIENT)
    log = common.setup_logging(cfg)
    code = common.proxy_cli(cfg, log, argv)
    if code is not None:
        return code
    if len(argv) > 1:
        print("usage: modelmatch_codex_hook.py [--start-proxy | --stop-proxy | --status]  (no args = hook mode)")
        return 2

    message = common.run_with_deadline(log, lambda: run_hook(cfg, log),
                                       "ModelMatch took too long, so your current model was kept.")
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
