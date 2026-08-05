# Docker Deployment

This project can be deployed using Docker and Docker Compose, making it suitable for deployment on platforms like elest.io.

## Quick Start

1. Copy `config.example.json` to `config.json` and configure for your Mobilizon instance
2. Set the `state_file` path to `/data/state.sqlite` in your config (for Docker volume persistence)
3. Optionally add images to an `images/` directory if using image mapping features
4. Run with Docker Compose:

```bash
docker-compose up -d
```

## Configuration

### Environment Variables

- `CRON_SCHEDULE`: Cron schedule for sync execution (default: `0 * * * *` = every hour)
- `MOBILIZON_EMAIL`: Your Mobilizon account email (REQUIRED for first run to obtain OAuth tokens)
- `MOBILIZON_PASSWORD`: Your Mobilizon account password (REQUIRED for first run to obtain OAuth tokens)

**Note:** These are your Mobilizon login credentials. The container uses them for password-based authentication with Mobilizon, which returns OAuth tokens. These OAuth tokens are then stored in the persistent volume for subsequent API calls. Your credentials are never stored in the database.

### Example .env file

```bash
CRON_SCHEDULE=0 * * * *
MOBILIZON_EMAIL=your-email@example.com
MOBILIZON_PASSWORD=your-password
```

## Volumes

The Docker Compose setup uses a named volume `mobilizon_sync_data` to persist the SQLite database:

- `/data` in the container is mounted to the `mobilizon_sync_data` volume
- This ensures your state and OAuth tokens persist across container restarts

## Authentication

The container automatically handles authentication via environment variables:

1. Set `MOBILIZON_EMAIL` and `MOBILIZON_PASSWORD` in docker-compose.yml or elest.io environment variables
2. On first start, the container will:
   - Register the OAuth app automatically
   - Login using the provided credentials
   - Store OAuth tokens in the persistent volume

**Token refresh:** The tool automatically refreshes access tokens using the stored refresh token during API calls. However, refresh tokens expire after 26 weeks (~6 months). When this happens, you'll need to restart the container to trigger re-login.

No manual init/login steps are required when using environment variables.

## Manual Sync

To trigger a sync manually without waiting for the cron schedule:

```bash
docker-compose run mobilizon-ical-sync mobilizon-event-sync -c /app/config.json sync
```

## Viewing Logs

View sync logs:

```bash
docker-compose logs -f
```

Or view the cron log file inside the container:

```bash
docker-compose exec mobilizon-ical-sync cat /var/log/sync.log
```

## Custom Cron Schedule

Change the sync frequency by setting `CRON_SCHEDULE` in your `.env` file or docker-compose.yml:

Examples:
- Every 30 minutes: `*/30 * * * *`
- Every 6 hours: `0 */6 * * *`
- Once daily at midnight: `0 0 * * *`
- Every Monday at 9am: `0 9 * * 1`

## Migrating Existing State

If you have an existing `state.sqlite` file from running the tool outside Docker, you need to export the event mappings to a SQL dump that can be committed to git for deployment.

**Important:** The export only includes the `event_map` and `media_cache` tables (event mappings and uploaded media). The `app` and `token` tables (containing OAuth credentials) are excluded for security and are instead handled via environment variables.

### Export Steps

1. Ensure you have your existing `state.sqlite` in the project directory
2. Run the export script:

```bash
./export_state.sh
```

This creates `state_dump.sql` which contains only your event mappings (ical_uid → event_uuid).

3. Commit the SQL dump to git:

```bash
git add state_dump.sql
git commit -m "Add event map and media cache dump for Docker deployment"
```

### How It Works

The Docker container will automatically:
- Check if `/data/state.sqlite` exists on first start
- If not, import `state_dump.sql` to create the event_map and media_cache tables
- If `MOBILIZON_EMAIL` and `MOBILIZON_PASSWORD` are set, auto-register the app and login to obtain OAuth tokens
- Store the obtained OAuth tokens in the persistent volume
- Subsequent runs will use the persisted tokens from the volume

This means:
- The SQL dump is baked into the Docker image (safe to commit - no secrets)
- On first deployment, it initializes the event mappings and media cache
- Mobilizon credentials are used to obtain OAuth tokens via environment variables
- OAuth tokens are stored in the persistent volume and refreshed automatically
- You only need to update the SQL dump when you want to reset the event mappings

### Local Testing with Migration

For local Docker testing with existing state:

```bash
# Export your current event mappings
./export_state.sh

# Set credentials in .env file
echo "MOBILIZON_EMAIL=your-email@example.com" > .env
echo "MOBILIZON_PASSWORD=your-password" >> .env

# Rebuild the Docker image with the new dump
docker-compose build

# Start the container (will import dump and auto-login)
docker-compose up -d
```

### Updating the State Dump

If you make changes locally and want to update the deployment:

```bash
# Export the updated event mappings
./export_state.sh

# Commit and push
git add state_dump.sql
git commit -m "Update event map and media cache dump"
git push
```

The next deployment will use the updated event mappings.

## Deployment on elest.io

### With Existing Event Mappings

1. Export your local event mappings to SQL:

```bash
./export_state.sh
```

2. Commit the SQL dump to git:

```bash
git add state_dump.sql
git commit -m "Add event map dump for deployment"
git push
```

3. In elest.io, create a new Docker Compose application
4. Point it to your repository
5. Set the following environment variables:
   - `MOBILIZON_EMAIL`: Your Mobilizon account email
   - `MOBILIZON_PASSWORD`: Your Mobilizon account password
   - `CRON_SCHEDULE`: (optional) Cron schedule for sync (default: every hour)
6. Deploy

The container will automatically:
- Import the event map and media cache dump on first start
- Register the OAuth app
- Login using the provided credentials to obtain OAuth tokens
- Store OAuth tokens in the persistent volume
- Start syncing according to the schedule

### Fresh Deployment (no existing state)

If you don't have existing event mappings or want to start fresh:

1. Ensure `state_dump.sql` does not exist (or is empty)
2. Push your code to a Git repository
3. In elest.io, create a new Docker Compose application
4. Point it to your repository
5. Set the following environment variables:
   - `MOBILIZON_EMAIL`: Your Mobilizon account email
   - `MOBILIZON_PASSWORD`: Your Mobilizon account password
   - `CRON_SCHEDULE`: (optional) Cron schedule for sync (default: every hour)
6. Deploy

The container will automatically:
- Register the OAuth app
- Login using the provided credentials to obtain OAuth tokens
- Store OAuth tokens in the persistent volume
- Start syncing according to the schedule
- Persist all state (including event mappings, media cache, and tokens) in the volume

The application will automatically:
- Start the cron daemon
- Run sync according to the schedule
- Persist database state in the volume
- Restart automatically if it crashes
