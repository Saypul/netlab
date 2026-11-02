#!/usr/bin/env bash
# Publish a NetLab release: ./release.sh 1.1.0 "what changed"
# Writes VERSION, commits it, tags vX.Y.Z (the notes become the update notes in NetLab) and pushes.
# Servers then see the update in the UI and with 'netlab update'.
set -euo pipefail
cd "$(dirname "$0")"
ver=${1:?usage: ./release.sh X.Y.Z \"release notes\"}
notes=${2:-NetLab $ver}
[[ $ver =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must look like 1.2.3" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "commit your changes first" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/v$ver" >/dev/null && { echo "v$ver already exists" >&2; exit 1; }
echo "$ver" > VERSION
git commit -qm "Release $ver" VERSION
git tag -a "v$ver" -m "$notes"
read -rp "Push v$ver to $(git remote get-url origin)? [y/N] " ok
[ "$ok" = y ] && git push origin HEAD "v$ver" || echo "Not pushed. Later: git push origin HEAD v$ver"
