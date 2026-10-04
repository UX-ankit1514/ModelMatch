#!/bin/bash
# Builds a clean, shareable zip in dist/ (nothing personal: no .env, .venv, logs, state or settings).
# Easiest way to share is the GitHub link; use this for sending a file (AirDrop, email, Slack).
#   bash scripts/package.sh
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
NAME="modelmatch-claude-code"
VERSION="$(python3 -c "import re,sys; print(re.search(r'__version__ = \"(.+?)\"', open(sys.argv[1]).read()).group(1))" "$REPO/proxy/__init__.py")"
OUT="$REPO/dist/$NAME-$VERSION.zip"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/$NAME" "$REPO/dist"
cd "$REPO"
# Allow-list: only these go into the zip.
for item in README.md docs requirements.txt pytest.ini .env.example .gitignore hooks proxy scripts tests; do
  cp -R "$item" "$STAGE/$NAME/"
done
find "$STAGE" \( -name __pycache__ -o -name .pytest_cache \) -type d -prune -exec rm -rf {} +
find "$STAGE" \( -name '*.pyc' -o -name .DS_Store -o -name '*.bak-*' \) -delete

# Safety scan: refuse to build if anything private slipped in.
BAD=""
if find "$STAGE" \( -name .env -o -name .venv -o -name logs -o -name state -o -name .claude -o -name dist \) | grep -q .; then BAD="private folders/files present"; fi
if grep -rIl --exclude=package.sh -E "/Users/[A-Za-z0-9._-]+|sk-ant-[A-Za-z0-9_-]{20,}|@arnifi" "$STAGE" >/dev/null 2>&1; then BAD="personal path, email or key found"; fi
if [ -n "$BAD" ]; then echo "STOP: $BAD. Zip NOT created."; grep -rIl --exclude=package.sh -E "/Users/[A-Za-z0-9._-]+|sk-ant-[A-Za-z0-9_-]{20,}|@arnifi" "$STAGE" || true; exit 1; fi

rm -f "$OUT"
(cd "$STAGE" && zip -qr "$OUT" "$NAME")
echo "Created: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Contents:"; unzip -Z1 "$OUT" | sed 's/^/  /'
