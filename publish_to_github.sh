#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${1:-}"

if [[ -z "$REPO_URL" ]]; then
  echo "Usage: ./publish_to_github.sh https://github.com/YOUR_USER/codex-prism-figure-bridge.git" >&2
  exit 2
fi

git branch -M main
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REPO_URL"
else
  git remote add origin "$REPO_URL"
fi

git push -u origin main
