#!/bin/bash
# ModelMatch for Codex CLI - uninstaller.
# Removes ONLY what install_codex.sh added: its two Codex hooks, its openai_base_url line
# (your previous one is put back), the background service, and (with --purge) its own logs
# and state files. Every Codex settings file is backed up first.
#
#   bash scripts/uninstall_codex.sh [--project DIR] [--purge]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT=""
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?--project needs a folder}"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
TOOL="$REPO/scripts/codex_settings_tool.py"
echo "ModelMatch for Codex - uninstall"
echo
if [ -n "$PROJECT" ]; then
  echo "1/3 Removing Codex hooks ($PROJECT)"
  python3 "$TOOL" uninstall "$PROJECT"
  echo
  echo "2/3 Model routing and the proxy are shared by all sessions: left as they are"
  echo "  (to remove them too: bash \"$REPO/scripts/uninstall_codex.sh\")"
else
  echo "1/3 Turning model routing off (Codex talks to its models directly again)"
  python3 "$TOOL" routing off
  python3 "$TOOL" uninstall --global
  echo
  echo "2/3 Stopping the background service and the local proxy"
  python3 "$REPO/scripts/agents_service.py" uninstall
  echo "  $(python3 "$REPO/hooks/modelmatch_codex_hook.py" --stop-proxy)"
  echo "  (if ModelMatch for Copilot is installed, its extension starts the proxy again when needed)"
fi
echo
echo "3/3 Local files"
if [ "$PURGE" = 1 ]; then
  rm -f "$REPO"/logs/codex-hook.log* "$REPO"/logs/agents-proxy.log* "$REPO"/logs/agents-proxy.out.log
  rm -rf "$REPO/state/handled-codex"
  echo "  removed Codex logs and state"
else
  echo "  kept logs/ and state/ (add --purge to delete Codex's files there)"
fi
echo
echo "Done. Restart any open Codex session so it stops using ModelMatch."
