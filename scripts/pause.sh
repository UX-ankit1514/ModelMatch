#!/bin/bash
# Pause ModelMatch: no popups or model switching until you run resume.sh.
# Claude Code keeps working normally. Takes effect from your next prompt.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$REPO/scripts/set_env.py" MODELMATCH_DISABLED 1 && echo "ModelMatch is paused. Resume any time with: bash \"$REPO/scripts/resume.sh\""
