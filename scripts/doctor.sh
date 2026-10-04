#!/bin/bash
# ModelMatch for Claude Code - health check. Changes nothing.
#
#   bash scripts/doctor.sh [--project DIR] [--tests]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$REPO"
RUN_TESTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?--project needs a folder}"; shift 2 ;;
    --tests) RUN_TESTS=1; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
PROBLEMS=0
ok()   { printf "  \xE2\x9C\x85 %s\n" "$*"; }
warn() { printf "  \xE2\x9A\xA0\xEF\xB8\x8F  %s\n" "$*"; }
bad()  { printf "  \xE2\x9D\x8C %s\n" "$*"; PROBLEMS=$((PROBLEMS + 1)); }
fix()  { printf "     fix: %s\n" "$*"; }
HOOK="$REPO/hooks/modelmatch_hook.py"

echo "ModelMatch - doctor"
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
  bad "proxy packages missing"; fix "bash \"$REPO/scripts/install.sh\""
fi
[ -f "$REPO/.env" ] && ok ".env present" || warn ".env missing (defaults are used)"

echo
jget() { printf '%s' "$1" | python3 -c "import sys,json; v=json.load(sys.stdin)['$2']; print(','.join(v) if isinstance(v, list) else (v or ''))" 2>/dev/null; }
PSTAT="$(python3 "$REPO/scripts/settings_tool.py" status "$PROJECT" 2>/dev/null)"
GSTAT="$(python3 "$REPO/scripts/settings_tool.py" status --global 2>/dev/null)"
PROXY_URL="$(jget "$PSTAT" proxy_url)"
PEV="$(jget "$PSTAT" hook_events)"; GEV="$(jget "$GSTAT" hook_events)"
PBASE="$(jget "$PSTAT" routing_base_url)"; GBASE="$(jget "$GSTAT" routing_base_url)"
BASE="${GBASE:-$PBASE}"
echo "Claude Code configuration"
if [ "$GEV" = "SessionStart,UserPromptSubmit" ]; then
  ok "ALL sessions: hooks installed in $(jget "$GSTAT" settings_file)"
else
  echo "     all sessions (global): not installed (bash \"$REPO/scripts/install.sh\" --global)"
fi
if [ "$PEV" = "SessionStart,UserPromptSubmit" ]; then
  ok "this folder ($PROJECT): hooks installed"
elif [ "$GEV" != "SessionStart,UserPromptSubmit" ]; then
  bad "hooks not installed (found: ${PEV:-none})"; fix "bash \"$REPO/scripts/install.sh\"   (or --global for every session)"
fi
if [ -z "$BASE" ]; then
  warn "model routing is OFF: recommendations are shown but not applied"
  fix "bash \"$REPO/scripts/install.sh\" (without --no-routing)"
elif [ "${BASE%/}" = "$PROXY_URL" ]; then
  ok "model routing is ON (ANTHROPIC_BASE_URL=$BASE)"
else
  warn "ANTHROPIC_BASE_URL points somewhere else ($BASE), so ModelMatch can't route"
fi

echo
echo "Local proxy"
if HEALTH="$(python3 "$HOOK" --status)"; then
  ok "running at $PROXY_URL"
  printf '%s' "$HEALTH" | python3 -c 'import sys,json; h=json.load(sys.stdin); print("     router: %s | upstream: %s | sessions: %s | up %ss" % (h["router"], h["upstream"], h["sessions"], h["uptime_seconds"]))'
else
  if [ -n "$BASE" ]; then
    bad "proxy NOT running, and routing is on, so Claude Code can't reach the API until it starts"
  else
    warn "proxy not running (it starts automatically when Claude Code starts)"
  fi
  fix "python3 \"$HOOK\" --start-proxy    (or switch routing off: bash \"$REPO/scripts/install.sh\" --no-routing)"
fi

echo
echo "Network"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://api.anthropic.com/v1/messages)"
if [ "$CODE" != "000" ]; then ok "Anthropic API reachable (HTTP $CODE is expected here)"; else bad "cannot reach api.anthropic.com"; fi
API_URL="$(cd "$REPO" && "$REPO/.venv/bin/python" -c 'from proxy.config import get_settings; print(get_settings().api_url)' 2>/dev/null)"
if [ -z "$API_URL" ]; then
  ok "router: built-in mock (MODELMATCH_API_URL is empty)"
else
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$API_URL")"
  if [ "$CODE" = "000" ]; then bad "router API not reachable: $API_URL"; elif [ "$CODE" = "404" ]; then bad "router API returns 404 (endpoint missing): $API_URL"; else ok "router API reachable: $API_URL (HTTP $CODE)"; fi
fi

echo
echo "Confirmation (Use / Keep)"
UI="$(cd "$REPO" && python3 -c 'import sys; sys.path.insert(0, "hooks"); import modelmatch_hook as h; print(h.Config().confirm_ui)' 2>/dev/null)"
echo "     setting: ${UI:-auto}"
if command -v osascript >/dev/null; then ok "macOS popup available"; else warn "macOS popup not available"; fi
echo "     note: Claude Code runs hooks without a terminal, so the [Y/n] terminal prompt can't appear there; the popup is used."

echo
echo "Recent problems in logs (last 5)"
LINES="$(cat "$REPO"/logs/hook.log "$REPO"/logs/proxy.log 2>/dev/null | grep -E 'ERROR|WARNING' | sort | tail -5)"
if [ -n "$LINES" ]; then printf '%s\n' "$LINES" | sed 's/^/     /'; else ok "none"; fi

if [ "$RUN_TESTS" = 1 ]; then
  echo
  echo "Automated tests"
  if (cd "$REPO" && "$REPO/.venv/bin/python" -m pytest -o addopts="" -q 2>&1 | tail -1); then :; fi
fi

echo
if [ "$PROBLEMS" = 0 ]; then echo "All good."; else echo "$PROBLEMS problem(s) found, see the 'fix' lines above."; fi
exit $(( PROBLEMS > 0 ))
