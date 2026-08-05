# mobilizon-event-sync

Sync events from various sources (iCal feeds, local files, or recurring patterns) into **Mobilizon** groups as native events. Runs as a one-shot CLI you can schedule via cron / systemd timer / k8s CronJob.

Features:

- OAuth 2.0 **Device Authorization Grant** against a Mobilizon instance — no password handling; user approves in a browser.
- Multi-organization: one config, many `(group, iCal feed)` pairs.
- Creates missing events, updates changed ones, and by default **cancels** (soft-remove) events that disappeared from the feed.
- Persists an iCal-UID → Mobilizon-event-UUID mapping plus OAuth tokens in a local SQLite state file. No shared state, no server needed.
- **Multiple event sources**: fetch from HTTP iCal URLs, local iCal files, or generate recurring events automatically.
- **Recurring event patterns**: define events that repeat on a schedule (e.g., first Friday of every month).
- **Organization activation**: enable/disable individual organizations without removing their configuration.

## Requirements

- Python 3.11+
- A Mobilizon account that is a member of the target group with sufficient rights (moderator/administrator) to organize events in that group.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

Copy the example and edit:

```bash
cp config.example.json config.json
```

Fields (as of current version):

| Key | Purpose |
| --- | --- |
| `instance_url` | Base URL of the Mobilizon instance (no trailing slash needed). |
| `app.name` / `app.website` | Metadata used when registering this tool as an OAuth application. |
| `state_file` | Path to the SQLite state DB (relative paths are resolved next to the config file). |
| `http_timeout_seconds` | HTTP timeout for both API calls and iCal fetches. |
| `organizations[].id` | Stable identifier for this org's token + event-mapping bucket. Change it and you'll re-sync from scratch. |
| `organizations[].group` | The group's `preferred_username` on the instance (the part before `@` in `@my-group@instance.tld`). |
| `organizations[].ical_url` | HTTPS URL to the `.ics` feed. One of `ical_url`, `ical_file`, or `recurring` must be specified. |
| `organizations[].ical_file` | Local file path to an `.ics` file. Alternative to `ical_url`. |
| `organizations[].recurring` | Configuration for automatically generating recurring events. Alternative to `ical_url`/`ical_file`. See below for pattern syntax. |
| `organizations[].active` | Whether this organization is enabled for syncing. Default: `true`. Set to `false` to temporarily disable without removing configuration. |
| `organizations[].actor_username` | Your **personal profile's** `preferred_username`. Events are organized as this identity and attributed to the group. |
| `organizations[].default_timezone` | IANA name used when the iCal event has no explicit timezone. |
| `organizations[].on_removed` | `cancel` (default), `delete`, or `ignore`. What to do with events that vanished from the feed. |
| `organizations[].visibility` | `PUBLIC`, `UNLISTED`, or `PRIVATE`. |
| `organizations[].category` | Mobilizon event category. Available options: `ARTS`, `BOOK_CLUBS`, `BUSINESS`, `CAUSES`, `COMEDY`, `CRAFTS`, `FOOD_DRINK`, `HEALTH`, `MUSIC`, `AUTO_BOAT_AIR`, `COMMUNITY`, `FAMILY_EDUCATION`, `FASHION_BEAUTY`, `FILM_MEDIA`, `GAMES`, `LANGUAGE_CULTURE`, `LEARNING`, `LGBTQ`, `MOVEMENTS_POLITICS`, `NETWORKING`, `PARTY`, `PERFORMING_VISUAL_ARTS`, `PETS`, `PHOTOGRAPHY`, `OUTDOORS_ADVENTURE`, `SPIRITUALITY_RELIGION_BELIEFS`, `SCIENCE_TECH`, `SPORTS`, `THEATRE`, `MEETING`. |
| `organizations[].tags` | Extra tags added to every synced event. |
| `organizations[].max_future_events` | Optional int. Sync only the N nearest upcoming events. `null` / omitted / `0` = no cap. Events beyond the cap are left untouched (not treated as removed). |
| `organizations[].default_location` | Optional default physical address applied to every synced event. Supports `description`, `street`, `locality`, `postal_code`, `region`, `country`, `timezone`, `latitude`, `longitude`, `geom`. |
| `organizations[].metadata` | Optional key-value pairs applied to every synced event. Each entry should have `key`, `value`, `title`, and `type` (uppercase: `STRING`, `INTEGER`, or `BOOLEAN`) fields. |
| `organizations[].default_image` | Optional default image filename (from `images/` folder) applied to every synced event. The image will be uploaded to Mobilizon automatically. |
| `organizations[].title_image_mapping` | Optional mapping of event titles to image filenames (from `images/` folder). Title must match precisely. Takes precedence over `default_image`. Images will be uploaded to Mobilizon automatically. Example: `{"Open Space/Chaostreff": "chaostreff.jpg"}`. |
| `organizations[].exclude_titles` | Optional list of glob patterns for event titles to exclude from sync. Example: `["*internal*", "*do not sync*"]`. |
| `organizations[].title_metadata_mapping` | Optional mapping of event title patterns to metadata. Pattern uses glob syntax. Example: `{"Open Space*": [{"key": "group", "title": "Chaotikum", "value": "https://chaotikum.org", "type": "STRING"}]}`. |
| `organizations[].location_mapping` | Optional mapping of location name patterns to structured addresses. Pattern uses glob syntax. Supports all address fields. |

## First-time setup

### Option 1: OAuth Device Flow (requires browser)

```bash
# 1. Register the app on the instance (creates client_id / client_secret in the state DB).
mobilizon-event-sync -c config.json init

# 2. For each organization, obtain OAuth tokens via device flow.
mobilizon-event-sync -c config.json login
# → prints a URL and a short code; open the URL, sign in, paste the code, approve.
```

### Option 2: Password-based Authentication (for server deployments)

Set your email and password as environment variables:

```bash
export MOBILIZON_EMAIL="your-email@example.com"
export MOBILIZON_PASSWORD="your-password"
```

Then run:

```bash
mobilizon-event-sync -c config.json login
```

The tool will automatically use password-based authentication when the environment variables are set. Tokens are stored locally and refreshed automatically when they expire (refresh tokens last 26 weeks).

## Sync

```bash
mobilizon-event-sync -c config.json sync             # all orgs
mobilizon-event-sync -c config.json sync --org my    # a single org
mobilizon-event-sync -c config.json sync --dry-run   # parse events, don't call the API
```

Exit code is non-zero if any per-event error occurred, so cron mails you on failure.

## Event sources

Each organization can use one of three event sources:

### 1. HTTP iCal URL (original)
Fetch events from a remote iCal feed:
```json
{
  "ical_url": "https://example.org/calendar.ics"
}
```

### 2. Local iCal file
Read events from a local file instead of fetching via HTTP:
```json
{
  "ical_file": "/path/to/local/calendar.ics"
}
```

### 3. Recurring event pattern
Automatically generate events that repeat on a schedule. Useful for regular meetings without an iCal feed:
```json
{
  "recurring": {
    "title": "Monthly Meeting",
    "description": "We meet every first Friday of the month at 18:00",
    "url": "https://example.org",
    "pattern": {
      "type": "monthly",
      "day_of_week": "friday",
      "week": "first",
      "time": "18:00"
    }
  }
}
```

**Pattern syntax:**
- `type`: Always `"monthly"` (currently only monthly patterns are supported)
- `day_of_week`: Day of the week - `monday`, `tuesday`, `wednesday`, `thursday`, `friday`, `saturday`, or `sunday`
- `week`: Which occurrence - `first`, `second`, `third`, `fourth`, or `last`
- `time`: Time in 24-hour format (HH:MM)

The system generates events starting from the current date up to `max_future_events` (default 12 if not specified).

### Example cron entry

```
*/15 * * * * cd /opt/mobilizon-event-sync && .venv/bin/mobilizon-event-sync -c config.json sync >> sync.log 2>&1
```

## Docker Deployment

A Docker image is available for containerized deployments. See [DOCKER.md](DOCKER.md) for detailed instructions.

Quick start:

```bash
docker-compose up -d
```

The container runs sync via cron (default: hourly) and persists state in a volume.

## What gets mapped

| iCal | Mobilizon |
| --- | --- |
| `UID` | Used as the stable key in the local mapping table. |
| `SUMMARY` | `title` |
| `DESCRIPTION` (+ `LOCATION` if free-text) | `description` |
| `DTSTART` / `DTEND` (or `DURATION`) | `beginsOn` / `endsOn` (ISO-8601 with offset) |
| `URL` or a URL in `LOCATION` | `onlineAddress` |
| `STATUS` (`CONFIRMED`/`TENTATIVE`/`CANCELLED`) | `status` |
| `CATEGORIES` | Merged into `tags` (plus any per-org tags in the config) |

Physical addresses aren't imported as structured Mobilizon addresses — Mobilizon expects a geocoded address object, which iCal doesn't provide. The free-text location is prepended to the description with a 📍 marker so it stays visible.

## OAuth scopes requested

- `read:group:events` — list existing events under a group
- `read:profile` — look up your own profile ID
- `write:event:create`, `write:event:update`, `write:event:delete` — the sync operations

## State file

A SQLite DB with four tables:

- `app` — one row per instance with the registered `client_id` / `client_secret`.
- `token` — one row per `(instance, org_id)` with access and refresh tokens.
- `event_map` — one row per `(org_id, ical_uid)` with the Mobilizon `event_uuid` and a content hash used to skip unchanged events.
- `media_cache` — one row per `(org_id, filename)` with the Mobilizon `media_uuid` for uploaded images.

Delete the state file to reset (you'll need to re-register the app and re-login).

## Caveats

- The tool only touches events it created itself (tracked via `event_map`). Existing events in the group are ignored.
- Recurring iCal events (`RRULE`) are **not expanded**; only the base event is synced. Mobilizon has no first-class recurrence yet, so the pragmatic approach for recurring calendars is to feed an already-expanded ICS.
- The default `on_removed: cancel` keeps the mapping alive so an event isn't cancelled twice. If the same UID reappears in the feed, it'll be brought back to `CONFIRMED` on the next diff.
