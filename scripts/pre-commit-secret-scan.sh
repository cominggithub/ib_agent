#!/usr/bin/env bash
# Refuse a commit that contains a real identifier.
#
# This repository is public, so every tracked file uses placeholders
# (agent-user, human-user, U1234567) while the real values live only in
# .secrets/, which is gitignored. Placeholders are easy to maintain and easy to
# forget, so the rule is enforced here rather than remembered.
#
# The patterns themselves are NOT in this file: it is tracked, and a tracked
# blocklist of secrets is just a slower leak. They come from
# .secrets/patterns.txt, which is ignored. No patterns file, no check - with a
# warning, so a fresh clone is not silently unprotected.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
PATTERNS="${REPO_ROOT}/.secrets/patterns.txt"

if [ ! -f "$PATTERNS" ]; then
  echo "pre-commit: no ${PATTERNS#"$REPO_ROOT/"}; secret scan skipped." >&2
  echo "pre-commit: see docs/SECRETS.md if this repository has real ids to protect." >&2
  exit 0
fi

# Only the staged content is scanned, and only what is being added: a pattern
# appearing in a deletion is the leak being removed.
staged="$(git diff --cached --unified=0 --no-color --diff-filter=ACMR)"
[ -n "$staged" ] || exit 0
added="$(printf '%s\n' "$staged" | grep -E '^\+' | grep -Ev '^\+\+\+' || true)"
[ -n "$added" ] || exit 0

found=0
while IFS= read -r pattern; do
  # skip blanks and comments
  case "$pattern" in ''|\#*) continue ;; esac
  if matches="$(printf '%s\n' "$added" | grep -nE "$pattern" || true)" && [ -n "$matches" ]; then
    if [ "$found" -eq 0 ]; then
      echo "pre-commit: REFUSED - staged changes contain real identifiers." >&2
      found=1
    fi
    echo "  pattern: ${pattern}" >&2
    # Show that a match happened without reprinting the secret itself.
    echo "    $(printf '%s\n' "$matches" | wc -l) line(s) matched" >&2
  fi
done < "$PATTERNS"

if [ "$found" -ne 0 ]; then
  cat >&2 <<'HINT'

Replace the real value with its placeholder from .secrets/IDENTIFIERS.md, or
move the content into .secrets/ (ignored). To find the offending lines:

  git diff --cached | grep -nE -f <(grep -vE '^\s*(#|$)' .secrets/patterns.txt)

This check is a safety net, not an authority: --no-verify bypasses it, and doing
so on a public repository is how account ids end up in history forever.
HINT
  exit 1
fi

exit 0
