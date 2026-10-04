#!/bin/bash
# ModelMatch for Codex CLI - installer (safe to run more than once).
#
#   bash scripts/install_codex.sh                     # every Codex session, with model routing
#   bash scripts/install_codex.sh --no-routing        # every Codex session, recommendations only
#   bash scripts/install_codex.sh --project ~/my-app  # one project's hooks (routing left as it is)
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT=""
ROUTING=1
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?--project needs a folder}"; shift 2 ;;
    --no-routing) ROUTING=0; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
ok()   { printf "  \xE2\x9C\x85 %s\n" "$*"; }
warn() { printf "  \xE2\x9A\xA0\xEF\xB8\x8F  %s\n" "$*"; }
fail() { printf "  \xE2\x9D\x8C %s\n" "$*"; exit 1; }
step() { printf "\n%s\n" "$*"; }
HOOK="$REPO/hooks/modelmatch_codex_hook.py"
TOOL="$REPO/scripts/codex_settings_tool.py"
SERVICE="$REPO/scripts/agents_service.py"

echo "ModelMatch for Codex - install"

step "1/7 Checking Python and Codex"
PY="$(command -v python3)" || fail "python3 not found. Install Apple's command line tools: xcode-select --install"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail "Python 3.9 or newer is needed"
ok "Python $("$PY" -c 'import platform; print(platform.python_version())')"
if command -v codex >/dev/null; then
  CODEX_VERSION="$(codex --version 2>/dev/null | head -1)"
  if printf '%s' "$CODEX_VERSION" | "$PY" -c 'import re,sys; m=re.search(r"(\d+)\.(\d+)\.(\d+)", sys.stdin.read()); sys.exit(0 if m and tuple(map(int, m.groups())) >= (0, 116, 0) else 1)'; then
    ok "$CODEX_VERSION"
  else
    warn "$CODEX_VERSION is too old for prompt hooks (0.116+ needed): run 'codex update'"
  fi
else
  warn "codex not found. Install Codex CLI first (npm install -g @openai/codex), then run this again."
fi

step "2/7 Installing the proxy's Python packages"
[ -x "$REPO/.venv/bin/python" ] || "$PY" -m venv "$REPO/.venv" || fail "could not create the .venv folder"
"$REPO/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1
"$REPO/.venv/bin/python" -m pip install --quiet -r "$REPO/requirements.txt" || fail "package install failed (is the internet on?)"
# zstandard reads Codex's compressed requests (ChatGPT sign-in); without it they pass through unswitched.
"$REPO/.venv/bin/python" -m pip install --quiet "zstandard>=0.22" || warn "zstandard didn't install: with a ChatGPT sign-in, models won't be switched"
ok "packages installed in .venv"

step "3/7 Local folders and settings"
mkdir -p "$REPO/logs" "$REPO/state" || fail "could not create logs/ and state/"
if [ ! -f "$REPO/.env" ]; then cp "$REPO/.env.example" "$REPO/.env" && ok "created .env (settings only, no secrets yet)"; else ok ".env already exists (left unchanged)"; fi

if [ -n "$PROJECT" ]; then
  step "4/7 Connecting Codex hooks (project: $PROJECT)"
  [ -d "$PROJECT" ] || fail "project folder not found: $PROJECT"
  "$PY" "$TOOL" install "$PROJECT" || fail "Codex settings were NOT changed"
else
  step "4/7 Connecting Codex hooks (ALL sessions, ~/.codex/hooks.json)"
  "$PY" "$TOOL" install --global || fail "Codex settings were NOT changed"
fi
ok "Codex hooks configured"

step "5/7 Starting the local proxy"
if [ -n "$PROJECT" ]; then
  "$PY" "$HOOK" --start-proxy >/dev/null || fail "proxy did not start - see logs/agents-proxy.out.log"
elif [ "$ROUTING" = 1 ]; then
  "$PY" "$TOOL" prepare-upstream || fail "could not read ~/.codex/config.toml"
  if [ "$(uname)" = "Darwin" ]; then
    "$PY" "$SERVICE" install || fail "the background service didn't start (details above)"
  else
    "$PY" "$HOOK" --stop-proxy >/dev/null 2>&1
  fi
  "$PY" "$HOOK" --start-proxy >/dev/null || fail "proxy did not start - see logs/agents-proxy.out.log"
else
  "$PY" "$TOOL" routing off || fail "could not change ~/.codex/config.toml"
  "$PY" "$SERVICE" uninstall >/dev/null
  "$PY" "$HOOK" --stop-proxy >/dev/null 2>&1
  "$PY" "$HOOK" --start-proxy >/dev/null || fail "proxy did not start - see logs/agents-proxy.out.log"
fi
for _ in $(seq 1 30); do "$PY" "$HOOK" --status >/dev/null 2>&1 && break; sleep 0.5; done
"$PY" "$HOOK" --status >/dev/null 2>&1 || fail "proxy is not answering - see logs/agents-proxy.out.log"
ok "proxy running"

step "6/7 Model routing"
if [ -n "$PROJECT" ]; then
  ok "left as it is (Codex reads routing only from ~/.codex/config.toml; run without --project to set it)"
elif [ "$ROUTING" = 1 ]; then
  "$PY" "$TOOL" routing on --require-proxy || fail "routing was NOT turned on"
else
  ok "off: recommendations only"
fi

step "7/7 Health check"
HEALTH="$("$PY" "$HOOK" --status)" || fail "proxy is not answering"
ok "proxy healthy: $HEALTH"
"$REPO/.venv/bin/python" - "$REPO" <<'EOF'
import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from proxy.agents.config import get_agent_settings
url = get_agent_settings(root=Path(sys.argv[1])).proxy_url + "/models/codex"
try:
    data = json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5))
except OSError as exc:
    print("  could not read Codex's models: %s" % exc); sys.exit(0)
tiers = data.get("tiers") or {}
print("  Codex models ModelMatch can switch to: %s" % (", ".join(data.get("models") or []) or "none found"))
print("  light: %s | standard: %s | heavy: %s" % (tiers.get("light", "-"), tiers.get("standard", "-"), tiers.get("heavy", "-")))
print("  (pin your own in .env, e.g. MODELMATCH_CODEX_LIGHT_MODEL=<model>)")
EOF

echo
echo "Done. Next steps:"
echo "  1. Quit any open Codex session and start a new one: codex"
echo "  2. Codex will say new hooks need review. Type /hooks, check the two 'ModelMatch' hooks"
echo "     (they run python3 \"$HOOK\") and trust them. Codex skips hooks until you do."
echo "  3. Type a prompt. A 'ModelMatch' popup suggests a model: click Use or Keep current."
echo "  Keep this folder where it is: Codex runs the hook from $REPO."
echo "  Problems? Run: bash \"$REPO/scripts/doctor_codex.sh\""
