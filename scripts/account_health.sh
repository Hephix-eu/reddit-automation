#!/bin/bash
# Emits one timestamped line per detected account-health issue.
# Output goes to stdout, captured by the Errors tab as a log source.

set -u
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ACCTS_DIR="/root/reddit-automation/accounts"

# 1. Locally-banned accounts (banned.json present)
for d in "$ACCTS_DIR"/*/; do
    [[ -d "$d" ]] || continue
    name=$(basename "$d")
    [[ "$name" == .* ]] && continue
    if [[ -f "$d/banned.json" ]]; then
        reason=$(python3 -c "import json,sys; d=json.load(open('$d/banned.json')); print(d.get('status','?'), '-', d.get('reason','no reason'))" 2>/dev/null || echo "banned.json unreadable")
        echo "$NOW  error  account=$name  BANNED: $reason"
    fi
done

# 2. Paused accounts (informational warning — distinguishes from broken)
for d in "$ACCTS_DIR"/*/; do
    [[ -d "$d" ]] || continue
    name=$(basename "$d")
    [[ "$name" == .* ]] && continue
    if [[ -f "$d/pause" ]]; then
        echo "$NOW  warn   account=$name  PAUSED (manual)"
    fi
done

# 3. MLX-orphan profiles (in MLX cloud but no local folder)
# We hardcode this from the audit since hitting MLX every page-load is expensive.
# Lives in a small JSON cache the audit script writes; for now, hardcode the known orphans.
ORPHANS_FILE=/var/lib/reddit-health/mlx-orphans.txt
if [[ -f "$ORPHANS_FILE" ]]; then
    while read -r line; do
        [[ -n "$line" ]] && echo "$NOW  warn   $line"
    done < "$ORPHANS_FILE"
fi
