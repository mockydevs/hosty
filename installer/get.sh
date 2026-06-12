#!/usr/bin/env bash
# Hosty single-command bootstrap. Intended usage (note the SHA-pinned URL):
#   HOSTY_REF=<FULL_40_CHARACTER_COMMIT_SHA>
#   curl --proto '=https' --tlsv1.2 -fsSL \
#     "https://raw.githubusercontent.com/mockydevs/hosty/$HOSTY_REF/installer/get.sh" \
#     | sudo env HOSTY_REF="$HOSTY_REF" bash
#
# Security model:
# - The download URL is pinned to the same immutable commit SHA that gets
#   installed, so this bootstrap and the installed tree come from one
#   revision — never a mutable branch like `main`.
# - All logic lives in main(), invoked only on the last line: a truncated
#   download parses but executes nothing.
# - This file only clones and verifies the pinned revision; all real work
#   runs from the git-verified checkout, and installer/install.sh
#   independently re-validates HOSTY_REF against `git rev-parse HEAD`.
set -euo pipefail

main() {
  # The caller may be sitting inside a directory the installer replaces.
  cd /

  [[ $EUID -eq 0 ]] || {
    echo "Run as root: pipe into 'sudo env HOSTY_REF=... bash'" >&2
    exit 1
  }

  local repo_url ref app_dir
  repo_url="${HOSTY_REPO_URL:-https://github.com/mockydevs/hosty.git}"
  ref="${HOSTY_REF:-}"
  [[ $ref =~ ^[0-9a-f]{40}$ ]] || {
    echo "HOSTY_REF must be an immutable full 40-character Git commit SHA" >&2
    exit 1
  }
  app_dir=/opt/hosty

  if ! command -v git >/dev/null; then
    apt-get update -q && apt-get install -qy git
  fi

  if [[ -d $app_dir/.git ]]; then
    git -C "$app_dir" fetch origin "$ref"
  else
    git clone --no-checkout "$repo_url" "$app_dir"
    git -C "$app_dir" fetch origin "$ref"
  fi
  git -C "$app_dir" checkout -q --detach "$ref"
  [[ $(git -C "$app_dir" rev-parse HEAD) == "$ref" ]] || {
    echo "Checked-out source does not match HOSTY_REF" >&2
    exit 1
  }

  export HOSTY_REF="$ref"
  exec bash "$app_dir/installer/install.sh"
}

main "$@"
