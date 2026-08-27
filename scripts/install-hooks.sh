#!/usr/bin/env bash
# Install the git hooks. Hooks live in .git/hooks, which is not tracked, so a
# fresh clone has none until this runs - hence an installer rather than a
# checked-in hook that only appears to be active.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_DIR="$(git -C "$REPO_ROOT" rev-parse --git-path hooks)"
HOOK_DIR="$(cd "$REPO_ROOT" && cd "$HOOK_DIR" && pwd)"

install_hook() {
  local name="$1" source="$2" target="${HOOK_DIR}/$1"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "refusing to overwrite an existing non-symlink hook: $target" >&2
    return 1
  fi
  ln -sfn "$source" "$target"
  chmod +x "$source"
  echo "installed $name -> ${source#"$REPO_ROOT/"}"
}

install_hook pre-commit "${REPO_ROOT}/scripts/pre-commit-secret-scan.sh"

if [ ! -f "${REPO_ROOT}/.secrets/patterns.txt" ]; then
  echo
  echo "note: .secrets/patterns.txt does not exist, so the scan will pass everything."
  echo "      See docs/SECRETS.md for what belongs in it."
fi
