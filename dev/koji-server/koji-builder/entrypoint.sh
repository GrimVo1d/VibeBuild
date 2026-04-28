#!/usr/bin/env bash
set -euo pipefail

echo "Waiting for Koji Hub..."
for i in $(seq 1 120); do
    if curl -sk https://koji-hub/kojihub >/dev/null 2>&1; then
        echo "Koji Hub is ready."
        break
    fi
    if [ "$i" -eq 120 ]; then
        echo "ERROR: Koji Hub did not become ready in 120 seconds"
        exit 1
    fi
    sleep 1
done

echo "Starting kojid..."
rm -f /var/run/kojid.pid
exec kojid --fg --verbose --force-lock
