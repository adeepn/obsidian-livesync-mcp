#!/usr/bin/env bash
set -euo pipefail

# No credentials or remote source content is printed by this checker.
trap 'printf "STATUS: ERROR\nUpstream check failed at line %s.\n" "$LINENO"; exit 4' ERR
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
pin="$root/docs/upstream-pin.md"
manifest="$root/docs/upstream-protocol-files.txt"
plugin=$(sed -n 's/^plugin: //p' "$pin")
commonlib=$(sed -n 's/^commonlib: //p' "$pin")
[[ "$plugin" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
latest=$(gh api repos/vrtmrz/obsidian-livesync/releases/latest --jq .tag_name)
head=$(gh api repos/vrtmrz/livesync-commonlib/commits/HEAD --jq .sha)
[[ "$head" =~ ^[0-9a-f]{40}$ ]]
[[ -n "$latest" && "$latest" != null ]]
if [[ "${1:-}" == --init-pin && "$commonlib" == uninitialized ]]; then
    # Explicit bootstrap only. Normal scheduled runs never advance the pin.
    sed "s/^commonlib: uninitialized$/commonlib: $head/" "$pin" > "$pin.tmp"
    mv -- "$pin.tmp" "$pin"
    commonlib=$head
fi
[[ "$commonlib" =~ ^[0-9a-f]{40}$ ]]
paths=()
while IFS= read -r path || [[ -n "$path" ]]; do
    [[ -z "$path" || "$path" == \#* ]] && continue
    [[ "$path" == src/* && "$path" != *..* && "$path" != *'*'* ]]
    paths+=("$path")
done < "$manifest"
[[ ${#paths[@]} -gt 0 ]]
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
git -C "$tmp" init -q
git -C "$tmp" fetch -q --depth=1 https://github.com/vrtmrz/livesync-commonlib.git "$commonlib"
if [[ "$head" != "$commonlib" ]]; then
    git -C "$tmp" fetch -q --depth=1 https://github.com/vrtmrz/livesync-commonlib.git "$head"
fi
# Reject missing paths in the baseline; deletions at HEAD remain real diffs.
for path in "${paths[@]}"; do
    git -C "$tmp" cat-file -e "$commonlib:$path"
done
changed=$(git -C "$tmp" diff --name-status "$commonlib" "$head" -- "${paths[@]}")
code=0
status=UNCHANGED
if [[ -n "$changed" ]]; then
    code=3
    status=PROTOCOL_DRIFT
elif [[ "${latest#v}" != "$plugin" ]]; then
    code=2
    status=RELEASE_DRIFT
fi
printf 'STATUS: %s\nPlugin: pinned %s; latest %s\nCommonlib: pinned %s; HEAD %s\n' \
    "$status" "$plugin" "$latest" "$commonlib" "$head"
printf 'Plugin compare: https://github.com/vrtmrz/obsidian-livesync/compare/%s...%s\n' "$plugin" "$latest"
printf 'Commonlib compare: https://github.com/vrtmrz/livesync-commonlib/compare/%s...%s\n' "$commonlib" "$head"
if [[ -n "$changed" ]]; then
    printf 'Protocol files changed (git name-status):\n%s\n' "$changed"
else
    printf 'Protocol files changed: none\n'
fi
exit "$code"
