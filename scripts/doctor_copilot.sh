#!/bin/bash
# ModelMatch for GitHub Copilot CLI - health check. Changes nothing.
#
#   bash scripts/doctor_copilot.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
case "${1:-}" in -h|--help) sed -n '2,4p' "$0"; exit 0 ;; esac
PROBLEMS=0
ok()   { printf "  \xE2\x9C\x85 %s\n" "$*"; }
warn() { printf "  \xE2\x9A\xA0\xEF\xB8\x8F  %s\n" "$*"; }
bad()  { printf "  \xE2\x9D\x8C %s\n" "$*"; PROBLEMS=$((PROBLEMS + 1)); }
fix()  { printf "     fix: %s\n" "$*"; }
HOOK="$REPO/hooks/modelmatch_copilot_hook.py"
jget() { printf '%s' "$1" | python3 -c "import sys,json; v=json.load(sys.stdin).get('$2'); print('' if v is None else v)" 2>/dev/null; }

echo "ModelMatch for Copilot CLI - doctor"
echo
echo "Software"
if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  ok "Python $(python3 -c 'import platform; print(platform.python_version())')"
else
  bad "Python 3.9+ not found"; fix "xcode-select --install"
fi
if "$REPO/.venv/bin/python" -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
  ok "proxy packages installed (.venv)"
else
  bad "proxy packages missing"; fix "bash \"$REPO/scripts/install_copilot.sh\""
fi
if command -v copilot >/dev/null; then
  VERSION="$(copilot --version 2>/dev/null | head -1)"
  if printf '%s' "$VERSION" | python3 -c 'import re,sys; m=re.search(r"(\d+)\.(\d+)\.(\d+)", sys.stdin.read()); sys.exit(0 if m and tuple(map(int, m.groups())) >= (1, 0, 44) else 1)'; then
    ok "$VERSION"
  else
    warn "$VERSION can't switch models mid-prompt (1.0.44+ needed)"; fix "copilot update"
  fi
else
  bad "copilot not found"; fix "npm install -g @github/copilot"
fi

echo
echo "Copilot configuration"
STAT="$(python3 "$REPO/scripts/copilot_settings_tool.py" status 2>/dev/null)"
if [ "$(jget "$STAT" extension_installed)" = "True" ]; then
  ok "extension installed in $(jget "$STAT" copilot_home)/extensions/modelmatch"
  if [ "$(jget "$STAT" extension_up_to_date)" != "True" ]; then
    warn "the installed extension is older than this folder's copy"; fix "bash \"$REPO/scripts/install_copilot.sh\""
  fi
  case "$(jget "$STAT" experimental)" in
    True) ok "experimental mode on (Copilot loads extensions)" ;;
    False) bad "experimental mode is off, so Copilot won't load the extension"; fix "start copilot and type /experimental on" ;;
    *) warn "couldn't read ~/.copilot/settings.json"; fix "start copilot and type /experimental on" ;;
  esac
elif [ "$(jget "$STAT" hooks_only_installed)" = "True" ]; then
  ok "command hooks installed (recommendations as notifications, no switching)"
else
  bad "ModelMatch is not installed in Copilot"; fix "bash \"$REPO/scripts/install_copilot.sh\""
fi
if [ "$(jget "$STAT" all_hooks_disabled)" = "True" ]; then
  bad "disableAllHooks is on in ~/.copilot/settings.json"; fix "remove \"disableAllHooks\" from that file"
fi

echo
echo "Local proxy"
if HEALTH="$(python3 "$HOOK" --status)"; then
  ok "running"
  printf '%s' "$HEALTH" | python3 -c 'import sys,json; h=json.load(sys.stdin); print("     router: %s | sessions: %s | up %ss" % (h["router"], h["sessions"], h["uptime_seconds"]))'
else
  warn "proxy not running (the extension starts it when Copilot starts)"
  fix "python3 \"$HOOK\" --start-proxy"
fi

echo
echo "Confirmation (Use / Keep)"
if command -v osascript >/dev/null; then ok "macOS popup available"; else warn "macOS popup not available (your model is always kept)"; fi

echo
echo "Recent problems in logs (last 5)"
LINES="$(cat "$REPO"/logs/copilot-hook.log "$REPO"/logs/copilot-extension.log "$REPO"/logs/agents-proxy.log 2>/dev/null | grep -E 'ERROR|WARNING' | sort | tail -5)"
if [ -n "$LINES" ]; then printf '%s\n' "$LINES" | sed 's/^/     /'; else ok "none"; fi

echo
if [ "$PROBLEMS" = 0 ]; then echo "All good."; else echo "$PROBLEMS problem(s) found, see the 'fix' lines above."; fi
exit $(( PROBLEMS > 0 ))
