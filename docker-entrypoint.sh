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

# Setup cron job for hourly sync
# CRON_SCHEDULE can be overridden via environment variable
CRON_SCHEDULE=${CRON_SCHEDULE:-"0 * * * *"}

# Create cron file
echo "$CRON_SCHEDULE cd /app && mobilizon-event-sync -c /app/config.json sync >> /var/log/sync.log 2>&1" > /etc/cron.d/mobilizon-sync

# Give cron file proper permissions
chmod 0644 /etc/cron.d/mobilizon-sync

# Apply cron job
crontab /etc/cron.d/mobilizon-sync

# Start cron in foreground
echo "Starting cron with schedule: $CRON_SCHEDULE"
cron -f
