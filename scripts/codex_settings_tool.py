#!/usr/bin/env python3
"""Safely add/remove ModelMatch entries in Codex CLI's settings.

  codex_settings_tool.py install   (--global | PROJECT_DIR)
  codex_settings_tool.py uninstall (--global | PROJECT_DIR)
  codex_settings_tool.py routing   (on | off) [--require-proxy]
  codex_settings_tool.py prepare-upstream
  codex_settings_tool.py status    (--global | PROJECT_DIR)

Scopes
  --global     hooks -> $CODEX_HOME/hooks.json (default ~/.codex/hooks.json): every Codex session.
  PROJECT_DIR  hooks -> PROJECT_DIR/.codex/hooks.json (loaded once you trust that project in Codex).
  routing      openai_base_url in $CODEX_HOME/config.toml, which Codex reads only from the
               user-level file, so it always applies to every Codex session.

Safety
  * Only hook entries whose command mentions modelmatch_codex_hook.py are touched.
  * config.toml is edited line by line: only a top-level openai_base_url line and Workflow
    Copilot's marker comments are added, replaced or removed. Everything else stays as it was.
  * A previous openai_base_url (another gateway, e.g. opencodex) becomes ModelMatch's
    upstream (MODELMATCH_CODEX_UPSTREAM_URL in .env) and is restored on uninstall.
  * Routing is refused when Codex uses a custom model_provider, or when a shell startup file
    sets OPENAI_BASE_URL (it would silently win over config.toml).
  * Every file is backed up (<file>.bak-<timestamp>) before it is changed; an unreadable or
    invalid file is never overwritten.
"""

import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from proxy.agents.codex_config import parse_scalar, scan_top_level  # noqa: E402

MARKER = "modelmatch_codex_hook.py"
EVENTS = {
    "SessionStart": {"matcher": "startup|resume|clear", "timeout": 30, "statusMessage": "ModelMatch: starting..."},
    "UserPromptSubmit": {"timeout": 120, "statusMessage": "ModelMatch: choosing the best model..."},
}
ROUTING_COMMENT = "# modelmatch: model routing through the local proxy (uninstall_codex.sh removes this)"
PREVIOUS_PREFIX = "# modelmatch: previous openai_base_url = "
SHELL_FILES = (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile")
UPSTREAM_KEY = "MODELMATCH_CODEX_UPSTREAM_URL"


def agent_settings():
    from proxy.agents.config import get_agent_settings
    return get_agent_settings(root=REPO)


def proxy_base_url():
    return agent_settings().proxy_url + "/v1"


def codex_home():
    return agent_settings().codex_home


def hooks_file(scope, project_dir):
    return codex_home() / "hooks.json" if scope == "global" else project_dir / ".codex" / "hooks.json"


def hook_command():
    # "|| true": exit code 2 means "block this prompt" to Codex, and Python exits 2 when the
    # script is missing, so a deleted or moved hook file must never be able to block Codex.
    return 'python3 "{}" || true'.format(REPO / "hooks" / MARKER)


def load_json(path):
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)  # raises on invalid JSON: we refuse to touch the file
    if not isinstance(data, dict):
        raise ValueError("{} is not a JSON object".format(path))
    return data


def backup(path):
    if path.exists():
        copy = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(str(path), str(copy))
        print("  backed up {} -> {}".format(path.name, copy.name))


def write_text(path, text):
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def is_ours(hook):
    return isinstance(hook, dict) and MARKER in str(hook.get("command", ""))


def remove_hooks(settings):
    removed = 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                kept = [h for h in group["hooks"] if not is_ours(h)]
                removed += len(group["hooks"]) - len(kept)
                if kept:
                    kept_groups.append(dict(group, hooks=kept))
            else:
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def our_events(settings):
    return sorted(event for event, groups in (settings.get("hooks") or {}).items()
                  if isinstance(groups, list)
                  and any(is_ours(h) for g in groups if isinstance(g, dict) for h in g.get("hooks", [])))


# ---------------------------------------------------------------------------
# config.toml (line-based edits of the top-level openai_base_url)
# ---------------------------------------------------------------------------


def read_config_text():
    path = codex_home() / "config.toml"
    if not path.exists():
        return path, ""
    return path, path.read_text(encoding="utf-8")


def top_level(text):
    keys, end = scan_top_level(text)
    return {key: (index, parse_scalar(raw)) for key, (index, raw) in keys.items()}, end


def toml_string(value):
    return json.dumps(value)  # a TOML basic string uses the same escapes for URLs


def set_base_url(text, url):
    """(new text, previous openai_base_url or None, changed?)."""
    lines = text.splitlines()
    keys, _ = top_level(text)
    if "openai_base_url" in keys:
        index, current = keys["openai_base_url"]
        if isinstance(current, str) and current.rstrip("/") == url.rstrip("/"):
            return text, None, False
        previous = current if isinstance(current, str) else None
        new = []
        if previous:
            new.append(PREVIOUS_PREFIX + toml_string(previous))
        new += [ROUTING_COMMENT, "openai_base_url = " + toml_string(url)]
        lines[index:index + 1] = new
    else:
        previous = None
        lines[0:0] = [ROUTING_COMMENT, "openai_base_url = " + toml_string(url)]
    return "\n".join(lines) + "\n", previous, True


def clear_base_url(text, url):
    """(new text, restored previous value or None, changed?). Only undoes our own change."""
    lines = text.splitlines()
    keys, end = top_level(text)
    previous = None
    for line in lines[:end]:
        if line.startswith(PREVIOUS_PREFIX):
            previous = parse_scalar(line[len(PREVIOUS_PREFIX):])
    ours = "openai_base_url" in keys and isinstance(keys["openai_base_url"][1], str) \
        and keys["openai_base_url"][1].rstrip("/") == url.rstrip("/")
    changed = False
    if ours:
        index = keys["openai_base_url"][0]
        if isinstance(previous, str) and previous:
            lines[index] = "openai_base_url = " + toml_string(previous)
        else:
            lines[index] = None
        changed = True
    for i in range(min(end, len(lines))):
        if lines[i] is not None and (lines[i].startswith(PREVIOUS_PREFIX) or lines[i] == ROUTING_COMMENT):
            lines[i] = None
            changed = True
    new_text = "\n".join(line for line in lines if line is not None)
    return (new_text + "\n" if new_text else ""), (previous if ours else None), changed


def shell_files_setting_base_url():
    found = []
    for name in SHELL_FILES:
        try:
            lines = (Path.home() / name).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        if any("OPENAI_BASE_URL" in line for line in lines if not line.lstrip().startswith("#")):
            found.append(name)
    return found


def set_env(key, value):
    import set_env as env_tool
    env_tool.main(["set_env.py", key, value])


def proxy_is_healthy():
    url = agent_settings().proxy_url + "/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as response:
            return json.loads(response.read()).get("service") == "modelmatch-agents-proxy"
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def install(scope, project_dir):
    path = hooks_file(scope, project_dir)
    settings = load_json(path)
    before = json.dumps(settings, sort_keys=True)
    remove_hooks(settings)  # idempotent: replace any previous version of our entries
    hooks = settings.setdefault("hooks", {})
    for event, options in EVENTS.items():
        handler = {"type": "command", "command": hook_command(), "timeout": options["timeout"],
                   "statusMessage": options["statusMessage"]}
        group = {"hooks": [handler]}
        if "matcher" in options:
            group = {"matcher": options["matcher"], "hooks": [handler]}
        hooks.setdefault(event, []).append(group)
    if json.dumps(settings, sort_keys=True) != before:
        write_text(path, json.dumps(settings, indent=2) + "\n")
        print("  hooks written to {}".format(path))
    else:
        print("  hooks already up to date in {}".format(path))
    _, text = read_config_text()
    if _hooks_feature_disabled(text):
        print("  NOTE: config.toml has hooks switched off ([features] hooks = false); Codex won't run them.")


def _hooks_feature_disabled(text):
    in_features = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_features = stripped.split("#")[0].strip() == "[features]"
            continue
        if in_features and "=" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition("=")
            if key.strip() in ("hooks", "codex_hooks") and parse_scalar(value) is False:
                return True
        if stripped.replace(" ", "") in ("features.hooks=false", "features.codex_hooks=false"):
            return True
    return False


def uninstall(scope, project_dir):
    path = hooks_file(scope, project_dir)
    settings = load_json(path)
    removed = remove_hooks(settings)
    if removed:
        write_text(path, json.dumps(settings, indent=2) + "\n")
        print("  removed {} ModelMatch hook(s) from {}".format(removed, path))
    else:
        print("  no ModelMatch hooks in {}".format(path))


def prepare_upstream():
    """Make the proxy's upstream the gateway Codex uses today (before routing is switched on)."""
    _, text = read_config_text()
    keys, _ = top_level(text)
    current = keys.get("openai_base_url", (None, None))[1]
    ours = proxy_base_url()
    if isinstance(current, str) and current.strip() and current.rstrip("/") != ours.rstrip("/"):
        set_env(UPSTREAM_KEY, current.strip().rstrip("/"))
        print("  Codex already uses a gateway: {} (ModelMatch will forward to it)".format(current))
    else:
        upstream = agent_settings().codex_upstream_url
        print("  upstream: {}".format(upstream or "automatic (ChatGPT sign-in -> chatgpt.com, API key -> api.openai.com)"))


def routing(state, require_proxy):
    config_path, text = read_config_text()
    url = proxy_base_url()
    if state == "on":
        keys, _ = top_level(text)
        provider = keys.get("model_provider", (None, None))[1]
        if isinstance(provider, str) and provider.strip() and provider.strip() != "openai":
            print("  NOT enabling routing: Codex uses a custom model_provider ({}).".format(provider))
            print("  Hooks are installed, so you get recommendations only.")
            return 0
        shells = shell_files_setting_base_url()
        if shells:
            print("  NOT enabling routing: ~/{} sets OPENAI_BASE_URL, which would override it.".format(shells[0]))
            print("  Hooks are installed, so you get recommendations only.")
            return 0
        if require_proxy and not proxy_is_healthy():
            print("  NOT enabling routing: the local proxy isn't answering at {}.".format(url))
            return 1
        new_text, previous, changed = set_base_url(text, url)
        if previous:
            set_env(UPSTREAM_KEY, previous.rstrip("/"))
            print("  kept your previous openai_base_url as ModelMatch's upstream: {}".format(previous))
        if changed:
            write_text(config_path, new_text)
            print("  model routing on: openai_base_url = {} in {}".format(url, config_path))
        else:
            print("  model routing already on ({})".format(url))
        return 0
    new_text, restored, changed = clear_base_url(text, url)
    if changed:
        write_text(config_path, new_text)
        if restored:
            print("  model routing off: openai_base_url restored to {}".format(restored))
        else:
            print("  model routing off (removed ModelMatch's openai_base_url)")
    else:
        print("  model routing was not on")
    return 0


def status(scope, project_dir):
    path = hooks_file(scope, project_dir)
    settings = load_json(path)
    _, text = read_config_text()
    keys, _ = top_level(text)
    base = keys.get("openai_base_url", (None, None))[1]
    provider = keys.get("model_provider", (None, None))[1]
    print(json.dumps({
        "scope": scope, "hook_events": our_events(settings), "hooks_file": str(path),
        "config_file": str(codex_home() / "config.toml"), "routing_base_url": base, "proxy_base_url": proxy_base_url(),
        "routing_on": isinstance(base, str) and base.rstrip("/") == proxy_base_url().rstrip("/"),
        "upstream": agent_settings().codex_upstream_url or "auto", "model_provider": provider,
        "hooks_feature_disabled": _hooks_feature_disabled(text), "shell_overrides": shell_files_setting_base_url(),
    }))


def main(argv):
    args = argv[1:]
    if not args or args[0] not in ("install", "uninstall", "routing", "prepare-upstream", "status"):
        print(__doc__)
        return 2
    action = args[0]
    try:
        if action == "prepare-upstream":
            prepare_upstream()
            return 0
        if action == "routing":
            if len(args) < 2 or args[1] not in ("on", "off"):
                print(__doc__)
                return 2
            return routing(args[1], "--require-proxy" in args)
        if len(args) < 2:
            print(__doc__)
            return 2
        scope = "global" if args[1] == "--global" else "project"
        project_dir = Path(args[1]).expanduser().resolve() if scope == "project" else None
        if scope == "project" and not project_dir.is_dir():
            print("  ERROR: project folder not found: {}".format(project_dir))
            return 1
        if action == "install":
            install(scope, project_dir)
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
