#!/bin/bash
# Hourly health check for ffassist.
# Restarts the service if it's inactive OR if its log hasn't been written to in
# >15 minutes (pollers normally print every 5 min, so silence = stuck).
# Posts a Slack notice when a restart happens.

set -u

LOG=/home/scott/FantasyFootball/data/service.log
UNIT=ffassist.service
STALE_SEC=900   # 15 min
NOTIFIER=/home/scott/FantasyFootball/.venv/bin/python

reason=""

if ! systemctl --user is-active --quiet "$UNIT"; then
    reason="service is inactive ($(systemctl --user is-active "$UNIT" 2>&1))"
elif [ ! -f "$LOG" ]; then
    reason="log file missing at $LOG"
else
    # File mtime check — works even if last line is buffered
    mtime=$(stat -c %Y "$LOG")
    now=$(date +%s)
    age=$((now - mtime))
    if [ "$age" -gt "$STALE_SEC" ]; then
        reason="log silent for ${age}s (>${STALE_SEC}s)"
    fi
fi

if [ -z "$reason" ]; then
    echo "[healthcheck] $(date -Is) ok"
    exit 0
fi

echo "[healthcheck] $(date -Is) RESTARTING — $reason"
systemctl --user restart "$UNIT"
restart_rc=$?
sleep 3
post_status=$(systemctl --user is-active "$UNIT")

# Slack ping (best-effort; ignore failure)
cd /home/scott/FantasyFootball || exit 0
set -a; . ./.env 2>/dev/null; set +a
"$NOTIFIER" - <<PY 2>&1 || true
from ffassist.slack_notify import SlackNotifier
SlackNotifier().channel(
    ":wrench: *ffassist auto-restart* — reason: \`$reason\`. "
    "Post-restart status: \`$post_status\` (rc=$restart_rc)."
)
PY
