#!/bin/bash
# Pull the latest papers/interests/groups databases from GitHub before running a scheduled task.
# Only updates papers_database.csv, interests_database.csv, and groups_database.csv — does NOT do a full git pull,
# which avoids merge conflicts with local code changes that may not yet be pushed.
#
# Exit codes:
#   0 — success (databases are now up-to-date with remote)
#   1 — fatal error (fetch failed, token missing, etc.)

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "==> [pull_from_github] Starting database sync from GitHub..."

# Remove git lock files left behind by crashed or interrupted git processes
# (index.lock, HEAD.lock, refs/heads/main.lock, packed-refs.lock, ...).
# Only locks older than 10 minutes are removed, so a git command that is
# running right now (e.g. from an editor) is never interrupted.
find "$DIR/.git" -name '*.lock' -type f -mmin +10 -print -delete 2>/dev/null \
  | sed 's/^/==> Removed stale lock: /' || true

# Build authenticated HTTPS remote URL.
TOKEN_FILE="$DIR/.github_token"
if [ -f "$TOKEN_FILE" ]; then
  TOKEN="$(cat "$TOKEN_FILE" | tr -d '[:space:]')"
  FETCH_REMOTE="https://${TOKEN}@github.com/sumanth-tangirala/scholar-dashboard.git"
  echo "==> Using HTTPS (token auth) for fetch..."
else
  echo "ERROR: .github_token not found at $TOKEN_FILE"
  echo "Cannot authenticate with GitHub. Aborting pull."
  exit 1
fi

# Fetch latest from remote (updates origin/main tracking ref).
echo "==> Fetching from origin..."
if ! git fetch "$FETCH_REMOTE" main:refs/remotes/origin/main 2>&1; then
  echo "ERROR: git fetch failed. Check network connectivity and token validity."
  exit 1
fi
echo "==> Fetch succeeded."

# --- Check and update each database file ---
UPDATED=0
DB_FILES=("papers_database.csv" "interests_database.csv" "groups_database.csv")

for FILE in "${DB_FILES[@]}"; do
  # Check if file differs between local working tree and origin/main.
  if git diff --quiet origin/main -- "$FILE" 2>/dev/null; then
    echo "==> $FILE is already up-to-date with remote."
  else
    # Determine which is newer: compare commit timestamps.
    LOCAL_DATE=$(git log -1 --format="%ct" -- "$FILE" 2>/dev/null || echo "0")
    REMOTE_DATE=$(git log -1 --format="%ct" origin/main -- "$FILE" 2>/dev/null || echo "0")

    if [ "$REMOTE_DATE" -gt "$LOCAL_DATE" ]; then
      echo "==> Remote has newer $FILE (remote: $REMOTE_DATE, local: $LOCAL_DATE). Updating..."
      # Use git show to extract the file content and write it directly,
      # avoiding the unlink permission issue with git checkout in some environments.
      git show "origin/main:$FILE" > "${FILE}.tmp" && mv "${FILE}.tmp" "$FILE"
      echo "==> $FILE updated from remote."
      UPDATED=$((UPDATED + 1))
    else
      echo "==> Local $FILE is newer or same age as remote (local: $LOCAL_DATE, remote: $REMOTE_DATE). Keeping local."
    fi
  fi
done

if [ "$UPDATED" -gt 0 ]; then
  echo "==> Sync complete. $UPDATED file(s) updated from GitHub."
else
  echo "==> Sync complete. No updates needed — local databases are current."
fi

echo "==> [pull_from_github] Done."
exit 0
