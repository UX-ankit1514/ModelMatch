#!/bin/bash
# ModelMatch for GitHub Copilot CLI - installer (safe to run more than once).
#
#   bash scripts/install_copilot.sh               # the extension: popup + model switching (recommended)
#   bash scripts/install_copilot.sh --hooks-only  # plain hooks: recommendations as notifications only
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODE="extension"
while [ $# -gt 0 ]; do
  case "$1" in
    --hooks-only) MODE="hooks-only"; shift ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
ok()   { printf "  \xE2\x9C\x85 %s\n" "$*"; }
warn() { printf "  \xE2\x9A\xA0\xEF\xB8\x8F  %s\n" "$*"; }
fail() { printf "  \xE2\x9D\x8C %s\n" "$*"; exit 1; }
step() { printf "\n%s\n" "$*"; }
HOOK="$REPO/hooks/modelmatch_copilot_hook.py"

echo "ModelMatch for Copilot CLI - install"

step "1/6 Checking Python and Copilot CLI"
PY="$(command -v python3)" || fail "python3 not found. Install Apple's command line tools: xcode-select --install"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail "Python 3.9 or newer is needed"
ok "Python $("$PY" -c 'import platform; print(platform.python_version())')"
if command -v copilot >/dev/null; then
  COPILOT_VERSION="$(copilot --version 2>/dev/null | head -1)"
  if printf '%s' "$COPILOT_VERSION" | "$PY" -c 'import re,sys; m=re.search(r"(\d+)\.(\d+)\.(\d+)", sys.stdin.read()); sys.exit(0 if m and tuple(map(int, m.groups())) >= (1, 0, 44) else 1)'; then
    ok "$COPILOT_VERSION"
  else
    warn "$COPILOT_VERSION can't switch models mid-prompt (1.0.44+ needed): run 'copilot update'"
    [ "$MODE" = "extension" ] && warn "until then you get recommendations only"
  fi
else
  warn "copilot not found. Install GitHub Copilot CLI first (npm install -g @github/copilot), then run this again."
fi

step "2/6 Installing the proxy's Python packages"
[ -x "$REPO/.venv/bin/python" ] || "$PY" -m venv "$REPO/.venv" || fail "could not create the .venv folder"
"$REPO/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1
"$REPO/.venv/bin/python" -m pip install --quiet -r "$REPO/requirements.txt" || fail "package install failed (is the internet on?)"
ok "packages installed in .venv"

step "3/6 Local folders and settings"
mkdir -p "$REPO/logs" "$REPO/state" || fail "could not create logs/ and state/"
if [ ! -f "$REPO/.env" ]; then cp "$REPO/.env.example" "$REPO/.env" && ok "created .env (settings only, no secrets yet)"; else ok ".env already exists (left unchanged)"; fi

if [ "$MODE" = "extension" ]; then
  step "4/6 Connecting Copilot (extension in ~/.copilot/extensions/modelmatch)"
  "$PY" "$REPO/scripts/copilot_settings_tool.py" install || fail "Copilot settings were NOT changed"
else
  step "4/6 Connecting Copilot (command hooks in ~/.copilot/hooks/modelmatch.json)"
  "$PY" "$REPO/scripts/copilot_settings_tool.py" install --hooks-only || fail "Copilot settings were NOT changed"
fi
ok "Copilot configured"

step "5/6 Starting the local proxy"
"$PY" "$HOOK" --start-proxy >/dev/null || fail "proxy did not start - see logs/agents-proxy.out.log"
ok "proxy running"

step "6/6 Health check"
HEALTH="$("$PY" "$HOOK" --status)" || fail "proxy is not answering"
ok "proxy healthy: $HEALTH"

echo
echo "Done. Next steps:"
echo "  1. Quit any open Copilot session and start a new one: copilot"
if [ "$MODE" = "extension" ]; then
  echo "  2. You should see 'ModelMatch is on'. (Type /extensions to see 'modelmatch' listed.)"
  echo "  3. Type a prompt. A 'ModelMatch' popup suggests a model: click Use or Keep current."
  echo "     With Use, the prompt is answered by that model and your model is put back afterwards."
else
  echo "  2. Type a prompt. A notification shows the recommended model; switch with /model if you agree."
fi
echo "  Keep this folder where it is: Copilot runs ModelMatch from $REPO."
echo "  Problems? Run: bash \"$REPO/scripts/doctor_copilot.sh\""
