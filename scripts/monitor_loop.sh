#!/bin/sh
set -u

INTERVAL="${MONITOR_INTERVAL:-300}"

case "$INTERVAL" in
    ''|*[!0-9]*)
        echo "MONITOR_INTERVAL must be a positive integer, got: $INTERVAL" >&2
        exit 1
        ;;
esac

if [ "$INTERVAL" -le 0 ]; then
    echo "MONITOR_INTERVAL must be greater than 0, got: $INTERVAL" >&2
    exit 1
fi

echo "Starting monitor loop, interval=${INTERVAL}s"

while true; do
    echo "[monitor] $(date +'%Y-%m-%d %H:%M:%S') Running /app/monitor_appointment.py"
    python /app/monitor_appointment.py || echo "[monitor] script exited with $?"
    sleep "$INTERVAL"
done
