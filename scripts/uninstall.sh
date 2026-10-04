#!/bin/bash
# ModelMatch for Claude Code - uninstaller.
# Removes ONLY what this project added: its two hooks, its ANTHROPIC_BASE_URL entry,
# and (with --purge) its own .venv, logs and state folders. Settings files are backed up first.
#
#   bash scripts/uninstall.sh [--global] [--project DIR] [--purge]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$REPO"
PURGE=0
GLOBAL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?--project needs a folder}"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    --global) GLOBAL=1; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
echo "ModelMatch - uninstall"
echo
if [ "$GLOBAL" = 1 ]; then
  echo "1/3 Removing Claude Code configuration (ALL sessions, user-level settings)"
  python3 "$REPO/scripts/settings_tool.py" uninstall --global
else
  echo "1/3 Removing Claude Code configuration ($PROJECT)"
  python3 "$REPO/scripts/settings_tool.py" uninstall "$PROJECT"
fi
echo
echo "2/3 Stopping the local proxy"
echo "  $(python3 "$REPO/hooks/modelmatch_hook.py" --stop-proxy)"
echo "  (if you installed both globally and per project, remove the other one too)"
echo
echo "3/3 Local files"
if [ "$PURGE" = 1 ]; then
  for d in .venv logs state; do
    if [ -d "$REPO/$d" ]; then rm -rf "${REPO:?}/$d" && echo "  removed $d/"; fi
  done
else
  echo "  kept .venv/, logs/ and state/ (add --purge to delete them)"
fi
echo
echo "Done. Restart any open Claude Code session so it stops using ModelMatch."
