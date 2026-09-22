#!/bin/bash
# Commit and push the latest papers/interests/groups data to GitHub.
# Run this after any Mode 1 (email digest) or Mode 2 (weekly survey) task completes.
# Usage: ./sync_to_github.sh [optional commit message]

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Default commit message includes today's date; override with first argument.
DATE="$(date '+%Y-%m-%d')"
MESSAGE="${1:-"Update papers and interests database - $DATE"}"

# Remove git lock files left behind by crashed or interrupted git processes
# (index.lock, HEAD.lock, refs/heads/main.lock, packed-refs.lock, ...).
# Only locks older than 10 minutes are removed, so a git command that is
# running right now (e.g. from an editor) is never interrupted.
find "$DIR/.git" -name '*.lock' -type f -mmin +10 -print -delete 2>/dev/null \
  | sed 's/^/==> Removed stale lock: /' || true

echo "==> Staging changes..."
git add papers_database.csv interests_database.csv groups_database.csv index.html CLAUDE.md

# Only commit if there are staged changes.
if git diff --cached --quiet; then
  echo "Nothing to commit — databases are already up to date."
  exit 0
fi

echo "==> Committing: \"$MESSAGE\""
git commit -m "$MESSAGE"

# Load GitHub PAT for HTTPS push (avoids SSH key requirement in automated sessions).
TOKEN_FILE="$DIR/.github_token"
if [ -f "$TOKEN_FILE" ]; then
  TOKEN="$(cat "$TOKEN_FILE" | tr -d '[:space:]')"
  HTTPS_REMOTE="https://${TOKEN}@github.com/sumanth-tangirala/scholar-dashboard.git"
  echo "==> Pulling remote changes (rebase)..."
  git pull --rebase "$HTTPS_REMOTE" main
  echo "==> Pushing via HTTPS (token auth)..."
  git push "$HTTPS_REMOTE" main
else
  echo "==> No .github_token found, attempting SSH push..."
  git pull --rebase origin main
  git push origin main
fi

echo "==> Done. Live at: https://www.sumanthtangirala.com/scholar-dashboard/"
