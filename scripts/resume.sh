#!/bin/bash
# Resume ModelMatch after pause.sh. Takes effect from your next prompt.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$REPO/scripts/set_env.py" MODELMATCH_DISABLED 0 && echo "ModelMatch is back on."
