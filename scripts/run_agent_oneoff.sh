#!/usr/bin/env bash
# run_agent_oneoff.sh — launch an ad-hoc Claude agent in the redditagent-image
# sidecar with the FULL warmup toolset, running a CUSTOM task prompt instead of
# the scheduled warmup flow. (Formalized from the one-off injection used to
# validate UI commenting; intended for the acctfarm "Agents" tab.)
#
# Usage:
#   scripts/run_agent_oneoff.sh <account> <prompt-file> [--warmup-prompt] [--bg]
#
#   # write a task, run it as the account, watch output:
#   printf 'open r/AskReddit and ... (use lib.browse for all interaction)\n' > /tmp/task.txt
#   scripts/run_agent_oneoff.sh swift_viper14 /tmp/task.txt
#
# Options:
#   --warmup-prompt  also append AGENT_PROMPT.md as the system prompt. Only for
#                    tasks that ARE a warmup session — its boot logic exits for
#                    graduated accounts and runs lock/scheduling. Default: OFF
#                    (the task file is the only instruction).
#   --bg             detach; stream to the log file only. Default: foreground+tee.
#
# Env: AGENT_IMAGE (default redditagent-image), MULTILOGIN_CONTAINER (default
#      multilogin), TIMEOUT_SEC (wall-clock kill, default 1800).
#
# Output streams to stdout AND accounts/<account>/agent_runs/<utc>.log.
# claude -p BUFFERS — expect no live output until it finishes; watch the
# screenshots dir / `docker exec <name> ps` for mid-run progress.
set -euo pipefail

REPO="/root/reddit-automation"
IMAGE="${AGENT_IMAGE:-redditagent-image}"
MLX_CONTAINER="${MULTILOGIN_CONTAINER:-multilogin}"
TIMEOUT_SEC="${TIMEOUT_SEC:-1800}"
CLAUDE_BIN="/root/.local/bin/claude"

usage() { sed -n '2,30p' "$0"; exit 2; }

[[ $# -lt 2 ]] && usage
ACCOUNT="$1"; shift
PROMPT_FILE="$1"; shift
APPEND_WARMUP=0
BG=0
for arg in "$@"; do
  case "$arg" in
    --warmup-prompt) APPEND_WARMUP=1 ;;
    --bg)            BG=1 ;;
    *) echo "unknown arg: $arg" >&2; usage ;;
  esac
done

[[ -d "$REPO/accounts/$ACCOUNT" ]] || { echo "no such account: $ACCOUNT" >&2; exit 2; }
[[ -f "$PROMPT_FILE" ]]           || { echo "prompt file not found: $PROMPT_FILE" >&2; exit 2; }
PROMPT_ABS="$(readlink -f "$PROMPT_FILE")"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "agent image '$IMAGE' missing" >&2; exit 1; }
docker ps --filter "name=$MLX_CONTAINER" --filter status=running --format '{{.Names}}' | grep -q . \
  || { echo "multilogin container '$MLX_CONTAINER' not running" >&2; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)_$$"   # _$$ keeps name/log unique on same-second launches
LOGDIR="$REPO/accounts/$ACCOUNT/agent_runs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/$STAMP.log"
NAME="agent_oneoff_${ACCOUNT}_${STAMP}"

# On SIGTERM/SIGINT (e.g. the dashboard "stop" button) tear down the container —
# otherwise killing this wrapper leaves `docker run` going to TIMEOUT_SEC.
cleanup() { trap - TERM INT; docker stop "$NAME" >/dev/null 2>&1 || true; exit 143; }
trap cleanup TERM INT

APPEND=""
[[ "$APPEND_WARMUP" == "1" ]] && APPEND="--append-system-prompt-file AGENT_PROMPT.md"

# $(cat ...) must be evaluated by the CONTAINER's bash, so escape the $ here.
INNER="cd /app && timeout ${TIMEOUT_SEC}s ${CLAUDE_BIN} -p --dangerously-skip-permissions --no-session-persistence ${APPEND} \"\$(cat /app/.agent_task.txt)\""

run() {
  docker run --rm --name "$NAME" \
    --network "container:$MLX_CONTAINER" \
    -v "$REPO/accounts:/app/accounts" \
    -v "$REPO/.env:/app/.env:ro" \
    -v "$REPO/lib:/app/lib:ro" \
    -v "$REPO/scripts:/app/scripts:ro" \
    -v "$REPO/fixtures:/app/fixtures:ro" \
    -v "$REPO/AGENT_PROMPT.md:/app/AGENT_PROMPT.md:ro" \
    -v "$PROMPT_ABS:/app/.agent_task.txt:ro" \
    -v /root/.claude:/root/.claude \
    -e IS_SANDBOX=1 -e RUNNING_IN_CONTAINER=1 \
    "$IMAGE" \
    bash -lc "$INNER"
}

echo "[run_agent_oneoff] account=$ACCOUNT prompt=$PROMPT_ABS warmup_prompt=$APPEND_WARMUP timeout=${TIMEOUT_SEC}s"
echo "[run_agent_oneoff] container=$NAME"
echo "[run_agent_oneoff] log=$LOG"

if [[ "$BG" == "1" ]]; then
  ( run >"$LOG" 2>&1 ) &
  echo "[run_agent_oneoff] detached (host pid $!). follow with: tail -f $LOG  | stop with: docker stop $NAME"
else
  run 2>&1 | tee "$LOG"
fi
