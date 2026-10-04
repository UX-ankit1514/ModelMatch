#!/bin/bash
# ModelMatch for Claude Code - installer (safe to run more than once).
#
#   bash scripts/install.sh                     # this folder only, with model routing
#   bash scripts/install.sh --global            # EVERY Claude Code session on this Mac
#   bash scripts/install.sh --no-routing        # recommendations only (add to any of the above)
#   bash scripts/install.sh --project ~/my-app  # one other project folder
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$REPO"
ROUTING=""
GLOBAL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?--project needs a folder}"; shift 2 ;;
    --no-routing) ROUTING="--no-routing"; shift ;;
    --global) GLOBAL=1; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
ok()   { printf "  \xE2\x9C\x85 %s\n" "$*"; }
warn() { printf "  \xE2\x9A\xA0\xEF\xB8\x8F  %s\n" "$*"; }
fail() { printf "  \xE2\x9D\x8C %s\n" "$*"; exit 1; }
step() { printf "\n%s\n" "$*"; }

echo "ModelMatch - install"

step "1/6 Checking Python"
PY="$(command -v python3)" || fail "python3 not found. Install Apple's command line tools: xcode-select --install"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail "Python 3.9 or newer is needed"
ok "Python $("$PY" -c 'import platform; print(platform.python_version())')"

step "2/6 Installing the proxy's Python packages"
[ -x "$REPO/.venv/bin/python" ] || "$PY" -m venv "$REPO/.venv" || fail "could not create the .venv folder"
"$REPO/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1
"$REPO/.venv/bin/python" -m pip install --quiet -r "$REPO/requirements.txt" || fail "package install failed (is the internet on?)"
ok "packages installed in .venv"

step "3/6 Local folders and settings"
mkdir -p "$REPO/logs" "$REPO/state" || fail "could not create logs/ and state/"
if [ ! -f "$REPO/.env" ]; then cp "$REPO/.env.example" "$REPO/.env" && ok "created .env (settings only, no secrets yet)"; else ok ".env already exists (left unchanged)"; fi
ok "logs/ and state/ ready"

if [ "$GLOBAL" = 1 ]; then
  step "4/6 Connecting Claude Code (ALL sessions, user-level settings)"
  "$PY" "$REPO/scripts/settings_tool.py" install --global $ROUTING || fail "Claude settings were NOT changed"
else
  step "4/6 Connecting Claude Code (project: $PROJECT)"
  [ -d "$PROJECT" ] || fail "project folder not found: $PROJECT"
  "$PY" "$REPO/scripts/settings_tool.py" install "$PROJECT" $ROUTING || fail "Claude settings were NOT changed"
fi
ok "Claude Code hooks configured"

step "5/6 Starting the local proxy"
# Restart so an update always runs the newest code.
"$PY" "$REPO/hooks/modelmatch_hook.py" --stop-proxy >/dev/null 2>&1
"$PY" "$REPO/hooks/modelmatch_hook.py" --start-proxy >/dev/null || fail "proxy did not start - see logs/proxy.out.log"
ok "proxy running"

step "6/6 Health check"
HEALTH="$("$PY" "$REPO/hooks/modelmatch_hook.py" --status)" || fail "proxy is not answering"
ok "proxy healthy: $HEALTH"

echo
echo "Done. Next steps:"
if [ "$GLOBAL" = 1 ]; then
  echo "  1. Quit your open Claude Code sessions and start new ones (any folder). Already-open sessions are unchanged."
  echo "  2. Keep this folder where it is: the hooks point to $REPO. If you move it, run this installer again."
else
  echo "  1. Quit any Claude Code session in $PROJECT and open a new one (cd \"$PROJECT\" && claude)."
fi
echo "  Then type a prompt. A 'ModelMatch' popup will suggest a model: click Use or Keep current."
echo "  Problems? Run: bash \"$REPO/scripts/doctor.sh\""
