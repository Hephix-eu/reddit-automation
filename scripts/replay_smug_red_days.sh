#!/usr/bin/env bash
# Replay smug_pickle72 Day 1, 4, 5, 8 — currently all show red on the Plan tab.
# Bump start_date so today=Day-N, fire session, restore at end.
set -u
REPO="/root/reddit-automation"
set_start() { python3 -c "import json; p='$REPO/accounts/$1/config.json'; c=json.load(open(p)); c['plan']['start_date']='$2'; open(p,'w').write(json.dumps(c, indent=2))"; }
force_now() { python3 -c "import json,datetime; nxt=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(minutes=1)).isoformat(); open('$REPO/accounts/$1/next_run.json','w').write(json.dumps({'next_run_utc':nxt,'reason':'$2','written_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}, indent=2))"; }

run_day() {
    local sd="$1" day="$2"
    echo
    echo "================================================================"
    echo "REPLAY smug Day $day (start_date=$sd) at $(date -u +%H:%M:%SZ)"
    echo "================================================================"
    set_start smug_pickle72 "$sd"
    force_now smug_pickle72 "replay_smug_day${day}"
    rm -f "$REPO/accounts/smug_pickle72/lock"
    "$REPO/scripts/run-on-host.sh" smug_pickle72
    echo "--- finished smug Day $day at $(date -u +%H:%M:%SZ) ---"
}

# today = 2026-05-22; day = (today - start_date).days + 1
run_day "2026-05-22" 1
run_day "2026-05-19" 4
run_day "2026-05-18" 5
run_day "2026-05-15" 8

# Restore to "real" current
set_start smug_pickle72 "2026-05-13"
echo
echo "RESTORE done — smug start_date=2026-05-13 (Day 10) at $(date -u)"
