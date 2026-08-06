#!/bin/bash

# Ensure config file exists
if [ ! -f /app/config.json ]; then
    echo "Error: config.json not found. Please mount a config file."
    exit 1
fi

# Import state dump if database doesn't exist
if [ ! -f /data/state.sqlite ] && [ -f /app/state_dump.sql ]; then
    echo "Importing existing event_map from state_dump.sql..."
    sqlite3 /data/state.sqlite < /app/state_dump.sql
    echo "✓ Event map imported successfully"
fi

# Auto-initialize app and login if credentials are provided
if [ -n "$MOBILIZON_EMAIL" ] && [ -n "$MOBILIZON_PASSWORD" ]; then
    echo "Credentials provided via environment variables"
    echo "Running init to register the app..."
    mobilizon-event-sync -c /app/config.json init

    echo "Running login to obtain tokens..."
    mobilizon-event-sync -c /app/config.json login
    echo "✓ App registered and logged in successfully"
fi

# Run sync in a loop
# SLEEP_INTERVAL can be overridden via environment variable (default: 1 hour)
SLEEP_INTERVAL=${SLEEP_INTERVAL:-3600}

echo "Starting sync loop with interval: ${SLEEP_INTERVAL}s"
while true; do
    echo "Running sync at $(date)"
    mobilizon-event-sync -c /app/config.json sync
    echo "Sync completed, sleeping for ${SLEEP_INTERVAL}s"
    sleep "$SLEEP_INTERVAL"
done
