#!/usr/bin/env bash
# Idempotent installer for the out-of-band watchdog systemd timer.
# Runs on hephix once after deploying watchdog.sh.

set -euo pipefail

REPO="${REPO:-/root/reddit-automation}"

cat > /etc/systemd/system/reddit-watchdog.service <<EOF
[Unit]
Description=Reddit warmup infrastructure watchdog
After=network.target docker.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart=$REPO/scripts/watchdog.sh
EOF

cat > /etc/systemd/system/reddit-watchdog.timer <<EOF
[Unit]
Description=Run reddit-watchdog every 30 minutes
Requires=reddit-watchdog.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
Persistent=true
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now reddit-watchdog.timer
systemctl list-timers reddit-watchdog.timer --no-pager
