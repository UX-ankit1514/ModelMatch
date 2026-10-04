#!/bin/bash
# ModelMatch for GitHub Copilot CLI - uninstaller.
# Removes ONLY what install_copilot.sh added: the extension (or the hooks file), and
# experimental mode if the installer turned it on. --purge also deletes its own logs and state.
#
#   bash scripts/uninstall_copilot.sh [--purge]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
echo "ModelMatch for Copilot CLI - uninstall"
echo
echo "1/3 Removing ModelMatch from Copilot"
python3 "$REPO/scripts/copilot_settings_tool.py" uninstall
echo
echo "2/3 The local proxy"
if [ -n "$(python3 "$REPO/scripts/codex_settings_tool.py" status --global 2>/dev/null | python3 -c 'import sys,json; print("x" if json.load(sys.stdin).get("routing_on") else "")' 2>/dev/null)" ]; then
  echo "  left running: ModelMatch for Codex uses it (uninstall_codex.sh stops it)"
else
  echo "  $(python3 "$REPO/hooks/modelmatch_copilot_hook.py" --stop-proxy)"
fi
echo
echo "3/3 Local files"
if [ "$PURGE" = 1 ]; then
  rm -f "$REPO"/logs/copilot-hook.log* "$REPO"/logs/copilot-extension.log
  rm -rf "$REPO/state/handled-copilot"
  echo "  removed Copilot logs and state"
else
  echo "  kept logs/ and state/ (add --purge to delete Copilot's files there)"
fi
echo
echo "Done. Restart any open Copilot session so it stops using ModelMatch."
