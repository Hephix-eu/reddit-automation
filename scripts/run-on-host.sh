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
INFRA_LOG="${INFRA_LOG:-/var/log/reddit-infra.log}"
ENV_FILE="${ENV_FILE:-/etc/reddit-agent.env}"

# Source Telegram creds if present (loaded into TELEGRAM_BOT_TOKEN/CHAT_ID)
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >>"$LOG"; }
infra_log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >>"$INFRA_LOG"; }

# Telegram alert. Throttles via a tiny per-message-key state file so we
# don't spam the channel if the same fault repeats every 15 min.
telegram() {
    local key="$1"; shift
    local msg="$*"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]] && return 0
    local state="/var/run/reddit-agent-tg-${key}.last"
    # Throttle: 4 hours between same-key alerts
    if [[ -f "$state" ]]; then
        local age=$(( $(date +%s) - $(stat -c %Y "$state" 2>/dev/null || echo 0) ))
        (( age < 14400 )) && return 0
    fi
    touch "$state"
    curl -s -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d text="$msg" >/dev/null 2>&1 || true
}

# Self-heal: rebuild image if missing. Always log the recovery.
ensure_image() {
    docker image inspect "$IMAGE" >/dev/null 2>&1 && return 0
    infra_log "image $IMAGE missing — auto-rebuilding"
    telegram "image_missing" "[hephix] redditagent-image was missing — auto-rebuilding now. Will resume scheduled sessions on next cron tick."
    if [[ ! -f "$REPO/Dockerfile.agent" ]]; then
        infra_log "rebuild FAILED — Dockerfile.agent not found at $REPO/Dockerfile.agent"
        telegram "rebuild_failed" "[hephix] ALERT: redditagent-image missing AND Dockerfile.agent not found at $REPO/Dockerfile.agent. Manual intervention needed."
        return 1
    fi
    if (cd "$REPO" && docker build -f Dockerfile.agent -t "$IMAGE" . >>"$INFRA_LOG" 2>&1); then
        infra_log "rebuild OK"
        return 0
    else
        infra_log "rebuild FAILED — see lines above"
        telegram "rebuild_failed" "[hephix] ALERT: redditagent-image rebuild FAILED. Check /var/log/reddit-infra.log on hephix."
        return 1
    fi
}

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

    # Skip if currently running (lock present, recent). The threshold is
    # comfortably above any normal session length — anything older is
    # definitely orphaned by a crashed wrapper or hung agent.
    #
    # Stale-lock path: don't just unlink the file. The MLX cloud-side lock
    # and any zombie Mimic process are still around, and the next agent
    # session will crash on profile-start (silent rc=1, no SQLite). Run
    # force_release first to stop the browser and release the cloud lock.
    if [[ -f "$lock" ]]; then
        local lock_age
        lock_age=$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || echo 0) ))
        if (( lock_age < 2700 )); then
            log "$user: lock present (age ${lock_age}s) — skipping"
            return 0
        fi
        log "$user: stale lock (${lock_age}s) — force-releasing MLX profile"
        if ! ensure_image; then
            log "$user: image unavailable — cannot force_release, skipping run"
            return 1
        fi
        if ! docker run --rm \
                --name "redditagent-${user}-release-$$" \
                --network "$NETWORK" \
                -v "$REPO/accounts:/app/accounts" \
                -v "$REPO/.env:/app/.env:ro" \
                "$IMAGE" \
                python3 scripts/force_release.py "$user" >>"$LOG" 2>&1; then
            log "$user: force_release FAILED — skipping run, will retry next cron tick"
            telegram "force_release_${user}" "[hephix] ${user}: stale lock cleanup couldn't reach MLX API. Will retry in 15min; manual check recommended if this repeats."
            return 1
        fi
        log "$user: force_release OK — removing lock file"
        rm -f "$lock"
    fi

    if [[ -f "$REPO/accounts/$user/banned.json" ]]; then
        log "$user: shadowbanned (banned.json present) — skipping automatic run"
        return 0
    fi

    if [[ -f "$REPO/accounts/$user/manual.json" ]]; then
        log "$user: manual mode — skipping automatic run"
        return 0
    fi

    if ! should_fire "$user"; then
        return 0
    fi

    # Self-heal image before firing
    if ! ensure_image; then
        log "$user: image rebuild failed — skipping run"
        return 1
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
