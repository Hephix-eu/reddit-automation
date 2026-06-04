#!/usr/bin/env bash
# One-shot replay: temporarily flip start_date for an account, fire run-on-host
# to log a fresh Day-N session, then move on. Restores both accounts to their
# "real" start_date at the end.
#
# Usage:
#   ./replay_days.sh
#
# Replays (in order):
#   smug_pickle72 Day 2  (start_date=2026-05-20)
#   smug_pickle72 Day 3  (start_date=2026-05-19)
#   dailyanvil    Day 1  (start_date=2026-05-21)
#   dailyanvil    Day 4  (start_date=2026-05-18)
# Then restores:
#   smug_pickle72        (start_date=2026-05-13 → Day 9)
#   dailyanvil           (start_date=2026-05-17 → Day 5)

set -u

REPO="/root/reddit-automation"

set_start() {
    local user="$1" sd="$2"
    python3 - <<PYEOF
import json
p = "$REPO/accounts/$user/config.json"
c = json.load(open(p))
c["plan"]["start_date"] = "$sd"
open(p, "w").write(json.dumps(c, indent=2))
PYEOF
}

force_now() {
    local user="$1" reason="$2"
    python3 - <<PYEOF
import json, datetime
nxt = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat()
open("$REPO/accounts/$user/next_run.json", "w").write(json.dumps({
    "next_run_utc": nxt,
    "reason": "$reason",
    "written_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, indent=2))
PYEOF
}

run_one() {
    local user="$1" start_date="$2" day="$3"
    echo
    echo "================================================================"
    echo "REPLAY: $user Day $day (start_date=$start_date)"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "================================================================"
    set_start "$user" "$start_date"
    force_now "$user" "replay_day${day}"
    rm -f "$REPO/accounts/$user/lock"
    "$REPO/scripts/run-on-host.sh" "$user"
    echo
    echo "--- finished $user Day $day at $(date -u +%H:%M:%SZ) ---"
}

run_one smug_pickle72 "2026-05-20" 2
run_one smug_pickle72 "2026-05-19" 3
run_one dailyanvil    "2026-05-21" 1
run_one dailyanvil    "2026-05-18" 4

# Restore to "real" current positions
echo
echo "================================================================"
echo "RESTORE: smug→Day9, dailyanvil→Day5"
echo "================================================================"
set_start smug_pickle72 "2026-05-13"
set_start dailyanvil    "2026-05-17"
echo
echo "ALL DONE at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
