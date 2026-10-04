#!/usr/bin/env python3
"""Install / remove ModelMatch in GitHub Copilot CLI's user settings.

  copilot_settings_tool.py install [--hooks-only]
  copilot_settings_tool.py uninstall
  copilot_settings_tool.py status

What it changes (all under $COPILOT_HOME, default ~/.copilot)
  extensions/modelmatch/   the extension (copied from hooks/copilot-extension/) plus a
                                 modelmatch.json that tells it where this folder is.
  settings.json                  "experimental": true, which Copilot needs to load extensions.
                                 Added only if missing; uninstall removes it only if this tool
                                 added it (remembered in state/copilot-install.json).
  hooks/modelmatch.json    --hooks-only: plain command hooks instead of the extension
                                 (recommendations as notifications, no switching, no
                                 experimental mode needed).

Safety
  * Only ModelMatch's own extension folder and hooks file are created or removed.
  * settings.json is backed up before it is changed, and never rewritten if it isn't plain
    JSON (comments would be lost); you are told to run /experimental on instead.
"""

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXTENSION_SOURCE = REPO / "hooks" / "copilot-extension" / "extension.mjs"
EXTENSION_NAME = "modelmatch"
MARKER_FILE = "modelmatch.json"
HOOK_SCRIPT = REPO / "hooks" / "modelmatch_copilot_hook.py"
HOOKS_FILE_NAME = "modelmatch.json"


def copilot_home():
    return Path(os.environ.get("COPILOT_HOME") or (Path.home() / ".copilot")).expanduser()


def state_file():
    return Path(os.environ.get("MODELMATCH_STATE_DIR") or (REPO / "state")) / "copilot-install.json"


def extension_dir():
    return copilot_home() / "extensions" / EXTENSION_NAME


def hooks_file():
    return copilot_home() / "hooks" / HOOKS_FILE_NAME


def settings_path():
    return copilot_home() / "settings.json"


def python_path():
    found = shutil.which("python3")
    return found or "python3"


def digest(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def backup(path):
    if path.exists():
        copy = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(str(path), str(copy))
        print("  backed up {} -> {}".format(path.name, copy.name))


def write_json(path, data, keep_backup=True):
    if keep_backup:
        backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_settings():
    """settings.json as a dict; raises ValueError if it can't be safely rewritten."""
    path = settings_path()
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)  # comments or invalid JSON: refuse to touch it
    if not isinstance(data, dict):
        raise ValueError("{} is not a JSON object".format(path))
    return data


def read_state():
    try:
        data = json.loads(state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(data):
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def ensure_experimental():
    try:
        settings = load_settings()
    except ValueError as exc:
        print("  NOTE: couldn't edit {} safely ({}).".format(settings_path(), exc))
        print("  Turn on extensions yourself: start copilot and type /experimental on")
        return False
    if settings.get("experimental") is True:
        print("  experimental mode already on in {}".format(settings_path()))
        return True
    settings["experimental"] = True
    write_json(settings_path(), settings)
    state = read_state()
    state["enabled_experimental"] = True
    write_state(state)
    print("  experimental mode on (needed for extensions) in {}".format(settings_path()))
    return True


def remove_extension():
    folder = extension_dir()
    if (folder / MARKER_FILE).exists():
        shutil.rmtree(str(folder))
        print("  removed the extension from {}".format(folder))
        return True
    return False


def remove_hooks_file():
    path = hooks_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if HOOK_SCRIPT.name in json.dumps(data):
        path.unlink()
        print("  removed {}".format(path))
        return True
    return False


def install_extension():
    folder = extension_dir()
    if folder.exists() and not (folder / MARKER_FILE).exists():
        raise ValueError("{} exists and isn't ModelMatch's; not touching it".format(folder))
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(EXTENSION_SOURCE), str(folder / "extension.mjs"))
    write_json(folder / MARKER_FILE, {"repo": str(REPO), "python": python_path(),
                                      "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")}, keep_backup=False)
    print("  extension installed in {}".format(folder))
    remove_hooks_file()
    ensure_experimental()


def install_hooks_only():
    command = 'python3 "{}" --event {{}} || true'.format(HOOK_SCRIPT)
    data = {"version": 1, "hooks": {
        "sessionStart": [{"type": "command", "bash": command.format("sessionStart"), "timeoutSec": 30}],
        "userPromptSubmitted": [{"type": "command", "bash": command.format("userPromptSubmitted"), "timeoutSec": 60}],
    }}
    path = hooks_file()
    write_json(path, data, keep_backup=path.exists())
    print("  command hooks written to {} (recommendations as notifications, no switching)".format(path))
    remove_extension()


def uninstall():
    removed = remove_extension()
    removed = remove_hooks_file() or removed
    if not removed:
        print("  ModelMatch was not installed in {}".format(copilot_home()))
    state = read_state()
    if state.get("enabled_experimental"):
        try:
            settings = load_settings()
            if settings.get("experimental") is True:
                del settings["experimental"]
                write_json(settings_path(), settings)
                print("  experimental mode off again (ModelMatch had turned it on)")
        except ValueError as exc:
            print("  NOTE: left {} as it is ({})".format(settings_path(), exc))
        state.pop("enabled_experimental", None)
        write_state(state)


def status():
    folder = extension_dir()
    try:
        settings = load_settings()
        experimental = settings.get("experimental") is True
        hooks_disabled = settings.get("disableAllHooks") is True
    except (ValueError, OSError):
        experimental, hooks_disabled = None, None
    installed = digest(folder / "extension.mjs")
    print(json.dumps({
        "copilot_home": str(copilot_home()),
        "extension_installed": (folder / MARKER_FILE).exists(),
        "extension_up_to_date": installed is not None and installed == digest(EXTENSION_SOURCE),
        "hooks_only_installed": hooks_file().exists() and HOOK_SCRIPT.name in hooks_file().read_text(encoding="utf-8"),
        "experimental": experimental,
        "all_hooks_disabled": hooks_disabled,
    }))


def main(argv):
    args = argv[1:]
    if not args or args[0] not in ("install", "uninstall", "status"):
        print(__doc__)
        return 2
    try:
        if args[0] == "install":
            if "--hooks-only" in args:
                install_hooks_only()
            else:
                install_extension()
        elif args[0] == "uninstall":
            uninstall()
        else:
            status()
    except (ValueError, OSError) as exc:
        print("  ERROR: {} (nothing else was changed)".format(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
