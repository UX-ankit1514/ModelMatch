#!/usr/bin/env python3
"""Safely add/remove ModelMatch entries in Claude Code settings files.

  settings_tool.py install   (--global | PROJECT_DIR) [--no-routing]
  settings_tool.py uninstall (--global | PROJECT_DIR)
  settings_tool.py status    (--global | PROJECT_DIR)

Scopes
  PROJECT_DIR  hooks -> PROJECT_DIR/.claude/settings.json
               routing (ANTHROPIC_BASE_URL) -> PROJECT_DIR/.claude/settings.local.json (git-ignored)
  --global     hooks and routing -> ~/.claude/settings.json (or $CLAUDE_CONFIG_DIR/settings.json),
               which applies to EVERY Claude Code session of this user.

Safety
  * Only entries whose command mentions modelmatch_hook.py are touched.
  * ANTHROPIC_BASE_URL is removed only if it still points at our proxy, and is never
    overwritten if something else (settings or shell startup files) already sets it.
  * Every file is backed up (<file>.bak-<timestamp>) before it is changed.
  * An unreadable/invalid settings file is never overwritten.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARKER = "modelmatch_hook.py"
EVENTS = {
    "SessionStart": {"timeout": 30, "statusMessage": "ModelMatch: starting..."},
    "UserPromptSubmit": {"timeout": 120, "statusMessage": "ModelMatch: choosing the best model..."},
}
SHELL_FILES = (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile")


def proxy_url():
    sys.path.insert(0, str(REPO))
    from proxy.config import get_settings
    return get_settings().proxy_url


def user_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def files_for(scope, project_dir):
    """(file holding hooks, file holding the routing env var)"""
    if scope == "global":
        settings = user_dir() / "settings.json"
        return settings, settings
    return project_dir / ".claude" / "settings.json", project_dir / ".claude" / "settings.local.json"


def hook_command(scope, project_dir):
    # "|| true": a missing/broken hook file must never be able to block Claude
    # (python exits with code 2 for a missing script, and 2 means "block").
    if scope == "project" and project_dir.resolve() == REPO:
        script = "${CLAUDE_PROJECT_DIR}/hooks/" + MARKER
    else:
        script = str(REPO / "hooks" / MARKER)
    return 'python3 "{}" || true'.format(script)


def load(path):
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)  # raises on invalid JSON: we refuse to touch the file
    if not isinstance(data, dict):
        raise ValueError("{} is not a JSON object".format(path))
    return data


def save(path, data):
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(str(path), str(backup))
        print("  backed up {} -> {}".format(path.name, backup.name))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def is_ours(hook):
    return isinstance(hook, dict) and MARKER in str(hook.get("command", ""))


def remove_hooks(settings):
    """Remove our hook entries; return how many were removed."""
    removed = 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                kept = [h for h in group["hooks"] if not is_ours(h)]
                removed += len(group["hooks"]) - len(kept)
                if kept:
                    new_groups.append(dict(group, hooks=kept))
            else:
                new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def shell_files_setting_base_url():
    found = []
    for name in SHELL_FILES:
        try:
            lines = (Path.home() / name).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        if any("ANTHROPIC_BASE_URL" in line for line in lines if not line.lstrip().startswith("#")):
            found.append(name)
    return found


def set_routing(settings, scope):
    """Turn on routing inside `settings` (a dict). Returns True if it changed."""
    url = proxy_url()
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    existing = env.get("ANTHROPIC_BASE_URL")
    if existing and existing.rstrip("/") != url:
        print("  NOT enabling routing: ANTHROPIC_BASE_URL is already set to {}".format(existing))
        print("  (set MODELMATCH_UPSTREAM_URL={} in .env and re-run to chain them)".format(existing))
        return False
    if existing:
        print("  model routing already on ({})".format(existing))
        return False
    if scope == "global":
        shells = shell_files_setting_base_url()
        if shells:
            print("  NOT enabling routing: ~/{} already sets ANTHROPIC_BASE_URL, and routing every".format(shells[0]))
            print("  session would override it. Hooks are installed, so you get recommendations only.")
            print("  (to chain them: set MODELMATCH_UPSTREAM_URL=<that url> in .env and re-run)")
            return False
    env["ANTHROPIC_BASE_URL"] = url
    settings["env"] = env
    print("  model routing on: ANTHROPIC_BASE_URL={}".format(url))
    return True


def clear_routing(settings):
    env = settings.get("env")
    if isinstance(env, dict) and str(env.get("ANTHROPIC_BASE_URL", "")).rstrip("/") == proxy_url():
        del env["ANTHROPIC_BASE_URL"]
        if not env:
            del settings["env"]
        return True
    return False


def install(scope, project_dir, routing):
    hooks_file, env_file = files_for(scope, project_dir)
    settings = load(hooks_file)
    before = json.dumps(settings, sort_keys=True)
    remove_hooks(settings)  # idempotent: replace any previous version of our entries
    hooks = settings.setdefault("hooks", {})
    for event, options in EVENTS.items():
        entry = {"type": "command", "command": hook_command(scope, project_dir)}
        entry.update(options)
        hooks.setdefault(event, []).append({"hooks": [entry]})

    same_file = env_file == hooks_file
    env_settings = settings if same_file else load(env_file)
    env_before = json.dumps(env_settings, sort_keys=True)
    if routing:
        set_routing(env_settings, scope)
    else:
        if clear_routing(env_settings):
            print("  model routing off (removed our ANTHROPIC_BASE_URL)")

    if json.dumps(settings, sort_keys=True) != before:
        save(hooks_file, settings)
        print("  hooks written to {}".format(hooks_file))
    else:
        print("  hooks already up to date in {}".format(hooks_file))
    if not same_file and json.dumps(env_settings, sort_keys=True) != env_before:
        save(env_file, env_settings)


def uninstall(scope, project_dir):
    hooks_file, env_file = files_for(scope, project_dir)
    settings = load(hooks_file)
    removed = remove_hooks(settings)
    same_file = env_file == hooks_file
    env_settings = settings if same_file else load(env_file)
    cleared = clear_routing(env_settings)
    if removed or (cleared and same_file):
        save(hooks_file, settings)
    if removed:
        print("  removed {} ModelMatch hook(s) from {}".format(removed, hooks_file))
    else:
        print("  no ModelMatch hooks in {}".format(hooks_file))
    if cleared:
        if not same_file:
            save(env_file, env_settings)
        print("  model routing off (removed ANTHROPIC_BASE_URL from {})".format(env_file.name))


def status(scope, project_dir):
    hooks_file, env_file = files_for(scope, project_dir)
    settings = load(hooks_file)
    env_settings = settings if env_file == hooks_file else load(env_file)
    events = sorted(e for e, groups in (settings.get("hooks") or {}).items()
                    if isinstance(groups, list)
                    and any(is_ours(h) for g in groups if isinstance(g, dict) for h in g.get("hooks", [])))
    base = (env_settings.get("env") or {}).get("ANTHROPIC_BASE_URL")
    print(json.dumps({"scope": scope, "hook_events": events, "routing_base_url": base,
                      "proxy_url": proxy_url(), "settings_file": str(hooks_file)}))


def main(argv):
    args = argv[1:]
    if len(args) < 2 or args[0] not in ("install", "uninstall", "status"):
        print(__doc__)
        return 2
    action, target = args[0], args[1]
    scope = "global" if target == "--global" else "project"
    project_dir = Path(target).expanduser().resolve() if scope == "project" else None
    try:
        if action == "install":
            install(scope, project_dir, routing="--no-routing" not in args)
        elif action == "uninstall":
            uninstall(scope, project_dir)
        else:
            status(scope, project_dir)
    except (ValueError, OSError) as exc:
        print("  ERROR: {} (no settings were changed)".format(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
