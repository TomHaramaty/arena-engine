#!/usr/bin/env bash
# Publish site/arena.json to arena-web, against a repo that moves.
#
# The floor is a build artifact: nothing is published until this file lands in
# arena-web AND its deploy re-renders. The tick pushes it up to ~38 times a day,
# so any other workflow doing the same thing can lose the race — clone, copy,
# commit, and find the branch has moved on.
#
# The retry loop below was written for first-bell (2026-07-28) after exactly that
# happened: an hourly tick landed between the clone and the push, the step
# failed, and the workflow wrote `failed` over a run that had SUCCEEDED. The
# other three workflows kept the unretried copy. This is that loop, once, for all
# of them.
#
# Never merge: arena.json is generated whole from the database, and ours is the
# later build. Rebuild on whatever head won and push that.
#
# Usage: ARENA_WEB_SSH_KEY=... bash tools/publish_arena.sh "data: tick refresh"
set -euo pipefail

MESSAGE="${1:-data: refresh}"
# ARENA_WEB_REPO exists so tests/test_publish.py can point this at a local bare
# repo and prove the race is really survived. CI never sets it.
REPO="${ARENA_WEB_REPO:-git@github.com:TomHaramaty/arena-web.git}"

if [ -n "${ARENA_WEB_SSH_KEY:-}" ]; then
  KEY="$HOME/.ssh/arena_web_key"
  mkdir -p "$HOME/.ssh"
  printf '%s' "$ARENA_WEB_SSH_KEY" > "$KEY"
  chmod 600 "$KEY"
  ssh-keyscan github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
  export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes"
fi

rm -rf webrepo
git clone -q --depth 1 "$REPO" webrepo
cd webrepo
git config user.name "arena-bot"
git config user.email "arena-bot@users.noreply.github.com"

for attempt in 1 2 3; do
  git fetch -q origin main
  git reset -q --hard origin/main
  cp ../site/arena.json data/arena.json
  git add data/arena.json
  if git diff --cached --quiet; then
    echo "arena.json: no data changes"
    exit 0
  fi
  git commit -q -m "$MESSAGE"
  if git push -q; then
    echo "arena.json: published"
    exit 0
  fi
  echo "push rejected (another workflow landed first) — rebuilding on the new head, attempt $attempt"
done

echo "could not publish arena.json after 3 attempts" >&2
exit 1
