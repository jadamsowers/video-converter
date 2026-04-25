#!/bin/bash

# Finds running ffmpeg processes and renices them to 10 if not already set.
# Loops every 10 seconds.

NICE_TARGET=10

echo "Monitoring ffmpeg processes (renice to ${NICE_TARGET})... Press Ctrl+C to stop."

while true; do
    # Get PIDs of all running ffmpeg processes
    PIDS=$(pgrep -x ffmpeg)

    if [ -z "$PIDS" ]; then
        echo "[$(date '+%H:%M:%S')] No ffmpeg processes found."
    else
        for PID in $PIDS; do
            # Get current nice value for this PID
            CURRENT_NICE=$(ps -o nice= -p "$PID" 2>/dev/null | tr -d ' ')

            if [ -z "$CURRENT_NICE" ]; then
                # Process may have exited between pgrep and ps
                continue
            fi

            if [ "$CURRENT_NICE" -eq "$NICE_TARGET" ]; then
                echo "[$(date '+%H:%M:%S')] PID $PID already at nice $NICE_TARGET — skipping."
            else
                echo "[$(date '+%H:%M:%S')] PID $PID nice=$CURRENT_NICE → renicing to $NICE_TARGET"
                renice -n "$NICE_TARGET" -p "$PID" 2>/dev/null && \
                    echo "[$(date '+%H:%M:%S')] PID $PID reniced successfully." || \
                    echo "[$(date '+%H:%M:%S')] PID $PID renice failed (may need sudo)."
            fi
        done
    fi

    sleep 10
done
