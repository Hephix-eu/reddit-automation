#!/usr/bin/env bash
# Out-of-band watchdog for the reddit warmup infrastructure.
#
# Runs on a systemd timer (NOT inside cron — must survive cron daemon failure).
# Every 30 min, checks: docker daemon, multilogin container, recent agent
# success per account. POSTs alerts to Telegram on degradation.
#
# Throttles each alert key to once per 4hr to avoid spam.

set -u

REPO="${REPO:-/root/reddit-automation}"
ENV_FILE="${ENV_FILE:-/etc/reddit-agent.env}"
INFRA_LOG="${INFRA_LOG:-/var/log/reddit-infra.log}"
MULTILOGIN_CONTAINER="${MULTILOGIN_CONTAINER:-multilogin}"
AGENT_IMAGE="${AGENT_IMAGE:-redditagent-image}"
STALE_HOURS=24    # alert if no successful session in this window per account

[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WATCHDOG: $*" >>"$INFRA_LOG"; }

telegram() {
    local key="$1"; shift
    local msg="$*"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]] && return 0
    local state="/var/run/reddit-watchdog-${key}.last"
    if [[ -f "$state" ]]; then
        local age=$(( $(date +%s) - $(stat -c %Y "$state" 2>/dev/null || echo 0) ))
        (( age < 14400 )) && return 0
    fi
    touch "$state"
    curl -s -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d text="$msg" >/dev/null 2>&1 || true
    log "telegram sent key=$key"
}

# --- Checks (in failure-cost order: cheapest first) ---

check_docker_daemon() {
    if ! docker info >/dev/null 2>&1; then
        log "docker daemon NOT responding"
        telegram "docker_down" "[hephix] ALERT: docker daemon is not responding. The whole pipeline is down until restored."
        return 1
    fi
}

check_multilogin() {
    if ! docker ps --filter "name=$MULTILOGIN_CONTAINER" --filter "status=running" --format '{{.Names}}' | grep -q .; then
        log "multilogin container NOT running"
        telegram "mlx_down" "[hephix] ALERT: multilogin container is not running. Browser sessions will fail. Restart with: docker start multilogin"
        return 1
    fi
}

check_image() {
    if ! docker image inspect "$AGENT_IMAGE" >/dev/null 2>&1; then
        log "agent image MISSING (will auto-rebuild on next agent run)"
        telegram "image_missing" "[hephix] WARN: $AGENT_IMAGE is gone. Next cron tick will auto-rebuild via run-on-host.sh — no action needed."
        return 1
    fi
}

check_cron_entry() {
    if [[ ! -f /etc/cron.d/reddit-agent ]]; then
        log "/etc/cron.d/reddit-agent MISSING"
        telegram "cron_entry_missing" "[hephix] ALERT: /etc/cron.d/reddit-agent is missing. Scheduled runs will not fire. Reinstall the cron entry."
        return 1
    fi
}

check_stale_accounts() {
    local now_epoch
    now_epoch=$(date +%s)
    local cutoff=$(( now_epoch - STALE_HOURS * 3600 ))
    for d in "$REPO"/accounts/*/; do
        local user
        user=$(basename "$d")
        [[ -f "$d/config.json" ]] || continue
        [[ -f "$d/pause" ]] && continue    # respect pause flag
        local db="$d/state.db"
        [[ -f "$db" ]] || continue
        local last
        last=$(docker run --rm -v "$REPO/accounts:/data" "$AGENT_IMAGE" \
            sqlite3 "/data/$user/state.db" \
            "SELECT MAX(executed_at) FROM actions_log WHERE type='Session' AND status='done'" 2>/dev/null || echo "")
        [[ -z "$last" ]] && continue
        local last_epoch
        last_epoch=$(date -d "$last" +%s 2>/dev/null || echo 0)
        if (( last_epoch < cutoff )); then
            log "$user: no successful Session in last $STALE_HOURS h (last=$last)"
            telegram "stale_$user" "[hephix] WARN: account '$user' has not completed a Session in over ${STALE_HOURS}h. Last success: $last. Check Multilogin profile or proxy."
        fi
    done
}

# --- Run checks ---
log "watchdog tick start"
check_docker_daemon || exit 0    # don't run further checks if daemon is dead
check_multilogin
check_image
check_cron_entry
check_stale_accounts
log "watchdog tick end"
