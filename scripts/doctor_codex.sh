#!/bin/bash
# ModelMatch for Codex CLI - health check. Changes nothing.
#
#   bash scripts/doctor_codex.sh [--project DIR]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?--project needs a folder}"; shift 2 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
PROBLEMS=0
ok()   { printf "  \xE2\x9C\x85 %s\n" "$*"; }
warn() { printf "  \xE2\x9A\xA0\xEF\xB8\x8F  %s\n" "$*"; }
bad()  { printf "  \xE2\x9D\x8C %s\n" "$*"; PROBLEMS=$((PROBLEMS + 1)); }
fix()  { printf "     fix: %s\n" "$*"; }
HOOK="$REPO/hooks/modelmatch_codex_hook.py"
TOOL="$REPO/scripts/codex_settings_tool.py"
jget() { printf '%s' "$1" | python3 -c "import sys,json; v=json.load(sys.stdin).get('$2'); print(','.join(v) if isinstance(v, list) else ('' if v is None else v))" 2>/dev/null; }

echo "ModelMatch for Codex - doctor"
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
  bad "proxy packages missing"; fix "bash \"$REPO/scripts/install_codex.sh\""
fi
if "$REPO/.venv/bin/python" -c "import zstandard" 2>/dev/null; then
  ok "zstandard installed (reads Codex's compressed requests)"
else
  warn "zstandard missing: with a ChatGPT sign-in, models can't be switched"; fix "bash \"$REPO/scripts/install_codex.sh\""
fi
if command -v codex >/dev/null; then ok "$(codex --version 2>/dev/null | head -1)"; else bad "codex not found"; fix "npm install -g @openai/codex"; fi

echo
echo "Codex configuration"
GSTAT="$(python3 "$TOOL" status --global 2>/dev/null)"
if [ -n "$PROJECT" ]; then PSTAT="$(python3 "$TOOL" status "$PROJECT" 2>/dev/null)"; else PSTAT=""; fi
GEV="$(jget "$GSTAT" hook_events)"; PEV="$(jget "$PSTAT" hook_events)"
if [ "$GEV" = "SessionStart,UserPromptSubmit" ]; then
  ok "ALL sessions: hooks installed in $(jget "$GSTAT" hooks_file)"
elif [ "$PEV" = "SessionStart,UserPromptSubmit" ]; then
  ok "project $PROJECT: hooks installed"
else
  bad "ModelMatch hooks not installed"; fix "bash \"$REPO/scripts/install_codex.sh\""
fi
echo "     reminder: Codex runs hooks only after you trust them. In Codex, type /hooks and trust the"
echo "     two 'ModelMatch' hooks (needed again if the hook file moves)."
if [ "$(jget "$GSTAT" hooks_feature_disabled)" = "True" ]; then
  bad "hooks are switched off in config.toml ([features] hooks = false)"; fix "remove that line from ~/.codex/config.toml"
fi
if [ "$(jget "$GSTAT" routing_on)" = "True" ]; then
  ok "model routing is ON (openai_base_url = $(jget "$GSTAT" routing_base_url))"
  echo "     forwards to: $(jget "$GSTAT" upstream)"
else
  warn "model routing is OFF: recommendations are shown but not applied"
  if [ -n "$(jget "$GSTAT" routing_base_url)" ]; then echo "     Codex uses another gateway: $(jget "$GSTAT" routing_base_url)"; fi
  fix "bash \"$REPO/scripts/install_codex.sh\" (without --no-routing)"
fi
if [ -n "$(jget "$GSTAT" shell_overrides)" ]; then
  warn "OPENAI_BASE_URL is set in ~/$(jget "$GSTAT" shell_overrides | cut -d, -f1); it overrides config.toml"
fi

echo
echo "Local proxy"
if HEALTH="$(python3 "$HOOK" --status)"; then
  ok "running"
  printf '%s' "$HEALTH" | python3 -c 'import sys,json; h=json.load(sys.stdin); print("     router: %s | codex upstream: %s | zstd: %s | sessions: %s | up %ss" % (h["router"], h["codex_upstream"], h["zstd"], h["sessions"], h["uptime_seconds"]))'
else
  if [ "$(jget "$GSTAT" routing_on)" = "True" ]; then
    bad "proxy NOT running, and routing is on, so Codex can't reach its models until it starts"
  else
    warn "proxy not running (it starts automatically when Codex starts)"
  fi
  fix "python3 \"$HOOK\" --start-proxy    (or switch routing off: bash \"$REPO/scripts/install_codex.sh\" --no-routing)"
fi
if [ "$(uname)" = "Darwin" ]; then
  SSTAT="$(python3 "$REPO/scripts/agents_service.py" status 2>/dev/null)"
  if [ "$(jget "$SSTAT" installed)" = "True" ]; then
    case "$(jget "$SSTAT" loaded)" in
      True)
        ok "background service loaded (keeps the proxy running)"
        if [ "$(jget "$SSTAT" runtime_current)" != "True" ]; then
          warn "the service runs an older copy of ModelMatch or of your .env settings"
          fix "python3 \"$REPO/scripts/agents_service.py\" restart"
        fi ;;
      False) bad "background service installed but not loaded"; fix "python3 \"$REPO/scripts/agents_service.py\" install" ;;
      *) echo "     background service file present (launchd not checked)" ;;
    esac
  elif [ "$(jget "$GSTAT" routing_on)" = "True" ]; then
    warn "no background service: after a restart, Codex may not reach its models until a hook starts the proxy"
    fix "bash \"$REPO/scripts/install_codex.sh\""
  fi
fi
MODELS="$(curl -s --max-time 5 "$(python3 -c "import sys; sys.path.insert(0, '$REPO/hooks'); import agents_common as c; print(c.AgentConfig('codex').proxy_url)")/models/codex")"
if [ -n "$MODELS" ]; then
  printf '%s' "$MODELS" | python3 -c 'import sys,json; d=json.load(sys.stdin); t=d.get("tiers") or {}; print("     models: %s" % (", ".join(d.get("models") or []) or "none found")); print("     light: %s | standard: %s | heavy: %s" % (t.get("light","-"), t.get("standard","-"), t.get("heavy","-")))' 2>/dev/null
fi

echo
echo "Confirmation (Use / Keep)"
if command -v osascript >/dev/null; then ok "macOS popup available"; else warn "macOS popup not available (your model is always kept)"; fi

echo
echo "Recent problems in logs (last 5)"
SVC_LOGS="$(jget "$(python3 "$REPO/scripts/agents_service.py" status 2>/dev/null)" runtime_dir)/logs"
LINES="$(cat "$REPO"/logs/codex-hook.log "$REPO"/logs/agents-proxy.log "$SVC_LOGS"/agents-proxy.log 2>/dev/null | grep -E 'ERROR|WARNING' | sort | tail -5)"
if [ -s "$SVC_LOGS/agents-proxy.out.log" ] && grep -q 'Error\|Traceback' "$SVC_LOGS/agents-proxy.out.log" 2>/dev/null; then
  LINES="$LINES
$(grep -E 'Error|Traceback' "$SVC_LOGS/agents-proxy.out.log" | tail -2)"
fi
if [ -n "$LINES" ]; then printf '%s\n' "$LINES" | sed 's/^/     /'; else ok "none"; fi

echo
if [ "$PROBLEMS" = 0 ]; then echo "All good."; else echo "$PROBLEMS problem(s) found, see the 'fix' lines above."; fi
exit $(( PROBLEMS > 0 ))
