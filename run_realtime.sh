#!/bin/bash
# Auto-restart wrapper for the realtime monitor.
# Keeps the process alive after crashes (OOM kill, transient errors, etc.).
# Usage:  nohup bash run_realtime.sh >> logs/scanner.log 2>&1 &

cd "$(dirname "$0")"

while true; do
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') | INFO | run_realtime | Starting realtime monitor..."
    python3 main.py realtime
    EXIT=$?
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') | WARNING | run_realtime | Monitor exited (code=$EXIT), restarting in 10s..."
    sleep 10
done
