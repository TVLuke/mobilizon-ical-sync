#!/bin/bash

# Export existing state.sqlite to SQL dump for deployment
# Only exports event_map (safe to commit), not app/token tables (contain secrets)

set -e

echo "=== Mobilizon iCal Sync - State Export Script ==="
echo ""

# Check if state.sqlite exists
if [ ! -f "state.sqlite" ]; then
    echo "Error: state.sqlite not found in current directory"
    echo "Please run this script from the mobilizon-ical-sync directory"
    exit 1
fi

echo "Found existing state.sqlite"

# Export only event_map and media_cache tables (safe to commit)
echo "Exporting event_map and media_cache to SQL dump..."
sqlite3 state.sqlite <<EOF > state_dump.sql
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS event_map (
    org_id        TEXT NOT NULL,
    ical_uid      TEXT NOT NULL,
    event_uuid    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (org_id, ical_uid)
);
CREATE TABLE IF NOT EXISTS media_cache (
    org_id      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    media_uuid  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (org_id, filename)
);
EOF

# Add INSERT statements for event_map
sqlite3 state.sqlite "SELECT 'INSERT INTO event_map VALUES(''' || org_id || ''',''' || ical_uid || ''',''' || event_uuid || ''',''' || content_hash || ''',' || updated_at || ');' FROM event_map;" >> state_dump.sql

# Add INSERT statements for media_cache
sqlite3 state.sqlite "SELECT 'INSERT INTO media_cache VALUES(''' || org_id || ''',''' || filename || ''',''' || media_uuid || ''',' || created_at || ');' FROM media_cache;" >> state_dump.sql

echo "COMMIT;" >> state_dump.sql

echo "✓ Export complete!"
echo ""
echo "Created state_dump.sql (event_map and media_cache)"
echo ""
echo "Note: app and token tables are excluded for security."
echo "The Docker container will auto-register the app and login via environment variables."
