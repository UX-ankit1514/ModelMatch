#!/usr/bin/env python3
"""ModelMatch for GitHub Copilot CLI: the decision-maker behind the extension.

Copilot runs this in one of two ways:

  --extension    from the ModelMatch extension (~/.copilot/extensions/modelmatch),
                 which sends one JSON object on stdin:
                   {"event": "session_start", "session_id", "cwd", "source", "model", "interactive", "can_switch"}
                   {"event": "prompt", "session_id", "cwd", "prompt", "current_model",
                    "available_models": [{"id", "name", "category"}], "interactive", "can_switch"}
                   {"event": "switch_result", "session_id", "ok", "detail"}
                 For a prompt it gets ONE recommendation, asks Use / Keep, saves the choice and
                 answers {"action": "switch" | "keep", "model_id", "model_name", "message"}.
                 The extension then switches Copilot's model for that prompt (Copilot's own
                 model switch) and back afterwards.

  --event NAME   as a plain Copilot command hook (~/.copilot/hooks/modelmatch.json, the
                 --hooks-only install; NAME is sessionStart or userPromptSubmitted). Command
                 hooks can't switch models or print under the prompt, so the recommendation is
                 shown as a macOS notification instead.

Golden rule: never get in Copilot's way. Every path exits 0 and prints at most one JSON
object; anything unexpected means "keep the current model".

Also usable from the command line: --start-proxy | --stop-proxy | --status

Standard library only, Python 3.7+.
"""

import json
import sys
import time

import agents_common as common

CLIENT = "copilot"
KEEP = "keep"
SWITCH = "switch"


def copilot_prompt_mode(command):
    """True for a `copilot -p ...` / `copilot --prompt ...` command line (a scripted run)."""
    words = command.split()
    return any("copilot" in word.lower() for word in words[:3]) and any(
        word in ("-p", "--prompt") or word.startswith("--prompt=") for word in words[1:])


def is_headless():
    return any(copilot_prompt_mode(command) for command in common.ancestor_commands())


def keep(message=None):
    return {"action": KEEP, "message": message}


# ---------------------------------------------------------------------------
# Extension mode
# ---------------------------------------------------------------------------


def session_start(cfg, log, data):
    session_id = data.get("session_id") or ""
    source = data.get("source") or "-"
    log.info("extension | session start | session=%s | source=%s | model=%s | interactive=%s",
             common.short(session_id), source, data.get("model") or "-", data.get("interactive"))
    if not common.ensure_proxy(cfg, log, wait_seconds=8):
        log.warning("fallback triggered | proxy unavailable at session start")
        return {"message": "ModelMatch: the local proxy couldn't start, so there will be no model "
                           "recommendations. Run: bash \"{}/scripts/doctor_copilot.sh\"".format(common.ROOT)}
    common.register_session(cfg, log, CLIENT, session_id, data.get("cwd"), source, data.get("model"))
    if source == "resume" or not data.get("interactive", True):
        return {}
    if not data.get("can_switch", True):
        return {"message": "ModelMatch is on (recommendations only: update Copilot CLI to 1.0.44 or newer "
                           "to switch models)."}
    return {"message": "ModelMatch is on (it suggests a model for each prompt; nothing changes unless you "
                       "click Use)."}


def decide(cfg, log, data):
    session_id = data.get("session_id") or "unknown"
    prompt = data.get("prompt")
    current = data.get("current_model") or None
    if not isinstance(prompt, str):
        log.warning("extension | prompt event without a prompt | session=%s", common.short(session_id))
        return keep()
    log.info("extension | prompt | session=%s | model=%s | models offered=%d | prompt=%d chars fp=%s | cwd=%s",
             common.short(session_id), current or "-", len(data.get("available_models") or []), len(prompt),
             common.fingerprint(prompt), data.get("cwd") or "-")
    if cfg.log_prompts:
        log.info("prompt preview | session=%s | %r", common.short(session_id), prompt[:80])
    if not prompt.strip():
        return keep()
    if prompt.lstrip().startswith("/"):
        log.info("skipped | slash command")
        return keep()
    if not data.get("interactive", True) and cfg.confirm_ui != "auto-accept":
        log.info("skipped | non-interactive run (copilot -p: no one to ask)")
        return keep()
    if not common.ensure_proxy(cfg, log, wait_seconds=5):
        log.warning("fallback triggered | proxy unavailable | keeping current model")
        return keep("ModelMatch is unavailable right now (local proxy not running). Continuing with your "
                    "current model.")

    rec, error = common.get_recommendation(cfg, log, {
        "client": CLIENT, "session_id": session_id, "prompt": prompt, "cwd": data.get("cwd"),
        "current_model": current, "available_models": data.get("available_models") or []})
    if rec is None:
        return keep("ModelMatch couldn't get a recommendation ({}). Continuing with your current model.".format(
            error))

    name = rec["recommended_model"]
    reason = rec.get("recommendation_reason") or ""
    reason_text = " " + reason if reason else ""
    substituted = (" (ModelMatch named {}; this is the closest model in your Copilot plan.)".format(
        rec["substituted_from"]) if rec.get("substituted_from") else "")

    def save(accepted, decided_by):
        return common.save_selection(cfg, log, CLIENT, session_id, rec, accepted, decided_by)

    if not rec.get("routable"):
        save(False, "unsupported")
        log.info("not applied | %s is not available: %s", name, rec.get("routing_note"))
        return keep("ModelMatch recommends {}.{} {} Keeping your current model.".format(
            name, reason_text, rec.get("routing_note") or "Copilot can't switch to it."))
    if rec.get("same_as_current"):
        save(False, "already-current")
        return keep("ModelMatch recommends {}, which is already your current model.".format(name))
    if not data.get("can_switch", True):
        save(False, "copilot-too-old")
        return keep("ModelMatch recommends {}.{}{} (Recommendation only: update Copilot CLI to 1.0.44 or "
                    "newer to switch models.)".format(name, reason_text, substituted))

    decision, method, detail = common.confirm(cfg, rec)
    accepted = decision == "accepted"
    result = save(accepted, method if decision in ("accepted", "rejected") else decision)
    log.info("user %s | session=%s | via %s%s", decision, common.short(session_id), method,
             " | " + detail if detail else "")

    if accepted and result.get("applied"):
        return {"action": SWITCH, "model_id": rec["model_id"], "model_name": name,
                "message": "ModelMatch: running this prompt on {}.{}{}".format(name, reason_text, substituted)}
    if accepted:
        log.warning("fallback triggered | accepted but the proxy did not record the route | keeping current model")
        return keep("ModelMatch couldn't apply {}. Keeping your current model.".format(name))
    if decision == "rejected":
        return keep("ModelMatch: kept your current model (recommended {}).".format(name))
    if decision == "timeout":
        return keep("ModelMatch: no answer within {:.0f}s, kept your current model (recommended {}).".format(
            cfg.confirm_timeout, name))
    log.warning("fallback triggered | confirmation UI unavailable: %s | keeping current model", detail)
    return keep("ModelMatch recommends {}.{} The Use/Keep question couldn't be shown, so your current model "
                "was kept.".format(name, reason_text))


def run_extension(cfg, log):
    data = common.read_stdin_json(log)
    if data is None:
        return keep()
    event = data.get("event")
    if event == "switch_result":
        if data.get("ok"):
            log.info("switched | session=%s | %s", common.short(data.get("session_id")), data.get("detail") or "")
        else:
            log.warning("fallback triggered | Copilot did not switch: %s | kept current model",
                        data.get("detail") or "unknown reason")
        return {}
    if cfg.disabled:
        log.info("paused via MODELMATCH_DISABLED | %s ignored", event)
        return keep() if event == "prompt" else {}
    if event == "session_start":
        return session_start(cfg, log, data)
    if event == "prompt":
        return decide(cfg, log, data)
    log.info("ignored extension event %s", event)
    return {}


# ---------------------------------------------------------------------------
# Command-hook mode (--hooks-only install)
# ---------------------------------------------------------------------------


def run_command_hook(cfg, log, event):
    data = common.read_stdin_json(log)
    if data is None or cfg.disabled:
        return None
    session_id = data.get("sessionId") or data.get("session_id") or ""
    if event == "sessionStart":
        log.info("hook received | sessionStart | session=%s | source=%s", common.short(session_id),
                 data.get("source") or "-")
        if common.ensure_proxy(cfg, log, wait_seconds=8):
            common.register_session(cfg, log, CLIENT, session_id, data.get("cwd"), data.get("source"), None)
        return None
    if event != "userPromptSubmitted":
        log.info("ignored event %s", event)
        return None
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or prompt.lstrip().startswith("/"):
        return None
    log.info("hook received | userPromptSubmitted | session=%s | prompt=%d chars fp=%s", common.short(session_id),
             len(prompt), common.fingerprint(prompt))
    if not common.claim(cfg, event, session_id, "{}-{}".format(common.fingerprint(prompt), int(time.time() // 10))):
        log.info("skipped | already handled by another copy of this hook")
        return None
    if is_headless():
        log.info("skipped | non-interactive run (copilot -p)")
        return None
    if not common.ensure_proxy(cfg, log, wait_seconds=5):
        return None
    rec, _ = common.get_recommendation(cfg, log, {"client": CLIENT, "session_id": session_id, "prompt": prompt,
                                                  "cwd": data.get("cwd")})
    if rec is None:
        return None
    common.save_selection(cfg, log, CLIENT, session_id, rec, False, "hooks-only")
    name = rec.get("recommended_raw") or rec["recommended_model"]
    if cfg.notify:
        common.notify("ModelMatch", "Recommended: " + name,
                      (rec.get("recommendation_reason") or "") + " Switch with /model if you agree.")
    log.info("recommendation shown as a notification | %s", name)
    return None


def main(argv):
    cfg = common.AgentConfig(CLIENT)
    log = common.setup_logging(cfg)
    code = common.proxy_cli(cfg, log, argv)
    if code is not None:
        return code

    if len(argv) == 2 and argv[1] == "--extension":
        answer = common.run_with_deadline(log, lambda: run_extension(cfg, log), keep(
            "ModelMatch took too long, so your current model was kept."))
        try:
            sys.stdout.write(json.dumps(answer if isinstance(answer, dict) else keep()))
            sys.stdout.flush()
        except Exception:
            pass
        return 0
    if len(argv) == 3 and argv[1] == "--event":
        common.run_with_deadline(log, lambda: run_command_hook(cfg, log, argv[2]), None)
        return 0
    print("usage: modelmatch_copilot_hook.py --extension | --event NAME | --start-proxy | --stop-proxy | --status")
    return 2


if __name__ == "__main__":
    try:
        code = main(sys.argv)
    except BaseException:
        code = 0
    sys.exit(code)
