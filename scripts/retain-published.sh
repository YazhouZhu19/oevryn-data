#!/usr/bin/env bash
# This script resets only the disposable Actions checkout, after publication verification.
# Parse the complete function before reset can replace the script in the working tree.
retain_published() (
  set -euo pipefail

  if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
    echo "Retention is restricted to the disposable GitHub Actions checkout." >&2
    exit 1
  fi
  : "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
  : "${RUNNER_TEMP:?RUNNER_TEMP is required}"

  workspace="$(cd "$GITHUB_WORKSPACE" && pwd -P)"
  checkout="$(git rev-parse --show-toplevel)"
  checkout="$(cd "$checkout" && pwd -P)"
  if [[ "$(pwd -P)" != "$workspace" || "$checkout" != "$workspace" ]]; then
    echo "Retention must run at the GitHub Actions workspace repository root." >&2
    exit 1
  fi
  temp_root="$(cd "$RUNNER_TEMP" && pwd -P)"
  case "$temp_root/" in
    "$workspace/"*)
      echo "RUNNER_TEMP must be outside the checkout so reset cannot remove the verified generation." >&2
      exit 1
      ;;
  esac
  if [[ ! -f next/deployment.json ]]; then
    echo "The verified next/deployment.json is missing." >&2
    exit 1
  fi

  protected="$(mktemp -d "$temp_root/oevryn-retain.XXXXXX")"
  trap 'rm -rf -- "$protected"' EXIT
  mkdir "$protected/next"
  rsync -a next/ "$protected/next/"

  git config user.name 'github-actions[bot]'
  git config user.email '41898282+github-actions[bot]@users.noreply.github.com'

  for attempt in 1 2 3; do
    echo "Retaining verified generation: attempt $attempt of 3."
    git fetch --depth=1 origin main
    git reset --hard origin/main
    mkdir -p published
    rsync -a --delete "$protected/next/" published/
    git add -- published/
    if git diff --cached --quiet; then
      echo "The verified generation is already retained on main."
      exit 0
    fi
    git commit -m 'chore: retain verified public data generation'
    if git push origin HEAD:main; then
      echo "Retained the verified generation on main."
      exit 0
    fi
  done

  echo "Unable to retain the verified generation after 3 normal push attempts." >&2
  exit 1
)

retain_published
