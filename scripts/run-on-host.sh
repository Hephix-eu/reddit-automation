#!/usr/bin/env bash
# Host-side wrapper for the reddit warmup agent.
#
# Invoked by /etc/cron.d/reddit-agent every 15min. For each account, checks
# `accounts/<user>/next_run.json` — if absent or its `next_run_utc` is in the
# past, fires one sidecar container run.
#
# Usage:
#   run-on-host.sh <username>         # run one account
#   run-on-host.sh --all              # iterate every account folder
#
# This script is the SOLE scheduler. The agent inside the container writes
# next_run.json after each session; this script reads it next cycle.

set -euo pipefail

REPO="${REPO:-/root/reddit-automation}"
IMAGE="${IMAGE:-redditagent-image}"
NETWORK="${NETWORK:-container:multilogin}"
LOG="${LOG:-/var/log/reddit-agent.log}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >>"$LOG"; }

# Should we fire a run for this account right now?
# Returns 0 (yes) if no next_run.json exists OR if its time has passed.
should_fire() {
    local marker="$REPO/accounts/$1/next_run.json"
    [[ ! -f "$marker" ]] && return 0    # never scheduled → fire (first run)
    local nxt
    nxt=$(python3 -c "import json,datetime;d=json.load(open('$marker'));print(d.get('next_run_utc',''))" 2>/dev/null || echo "")
    [[ -z "$nxt" ]] && return 0
    # Compare next_run_utc with now (both ISO8601 UTC). String compare works for ISO.
    local now
    now=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
    [[ "$nxt" < "$now" ]]
}

run_one() {
    local user="$1"
    local lock="$REPO/accounts/$user/lock"

    # Skip if currently running (lock present, recent)
    if [[ -f "$lock" ]]; then
        local lock_age
        lock_age=$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || echo 0) ))
        if (( lock_age < 7200 )); then
            log "$user: lock present (age ${lock_age}s) — skipping"
            return 0
        fi
        log "$user: stale lock (${lock_age}s) — removing"
        rm -f "$lock"
    fi

    if ! should_fire "$user"; then
        return 0
    fi

    log "$user: firing agent session"
    docker run --rm \
        --name "redditagent-$user-$$" \
        --network "$NETWORK" \
        -v "$REPO/accounts:/app/accounts" \
        -v "$REPO/.env:/app/.env:ro" \
        -v "/root/.claude:/root/.claude" \
        -e IS_SANDBOX=1 \
        -e RUNNING_IN_CONTAINER=1 \
        "$IMAGE" \
        python3 cli.py run "$user" >>"$LOG" 2>&1 || \
        log "$user: agent exited non-zero (rc=$?)"
}

if [[ "${1:-}" == "--all" ]]; then
    for d in "$REPO"/accounts/*/; do
        user=$(basename "$d")
        [[ -f "$d/config.json" ]] || continue
        run_one "$user"
    done
else
    [[ -z "${1:-}" ]] && { echo "usage: $0 <username> | --all"; exit 2; }
    run_one "$1"
fi
