"""Config loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class AppInfo:
    name: str
    website: str | None = None


@dataclass(frozen=True)
class Location:
    """A default physical address for events.

    Mirrors Mobilizon's `AddressInput` fields. Provide either `geom`
    (raw "lon;lat" string) or both `latitude` and `longitude`.
    """
    description: str | None = None  # short label (e.g. venue name)
    street: str | None = None
    locality: str | None = None
    postal_code: str | None = None
    region: str | None = None
    country: str | None = None
    type: str | None = None
    url: str | None = None
    timezone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    geom: str | None = None  # raw "lon;lat" if you prefer
    max_participants: int | None = None

    def to_address_input(self) -> dict | None:
        payload: dict = {}
        for key in (
            "description", "street", "locality", "postal_code",
            "region", "country", "type", "url", "timezone",
        ):
            v = getattr(self, key)
            if v is not None:
                payload[_to_camel(key)] = v
        geom = self.geom
        if geom is None and self.latitude is not None and self.longitude is not None:
            geom = f"{self.longitude};{self.latitude}"
        if geom:
            payload["geom"] = geom
        return payload or None


def _to_camel(snake: str) -> str:
    head, *tail = snake.split("_")
    return head + "".join(p.capitalize() for p in tail)


@dataclass(frozen=True)
class RecurringPattern:
    """Pattern for recurring events."""
    type: Literal["monthly"]
    day_of_week: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    week: Literal["first", "second", "third", "fourth", "last"]
    time: str  # HH:MM format


@dataclass(frozen=True)
class RecurringConfig:
    """Configuration for a recurring event."""
    title: str
    description: str
    url: str | None
    pattern: RecurringPattern


@dataclass(frozen=True)
class OrgConfig:
    id: str
    group: str  # federated username of the group on this instance, e.g. "my-group"
    ical_url: str | None = None  # URL to fetch iCal from
    ical_file: str | None = None  # Local file path to iCal file
    recurring: RecurringConfig | None = None  # Recurring event configuration
    active: bool = True  # Whether this organization is enabled for syncing
    # The profile (preferred username) that will organize events.
    # Leave empty/None to use the logged-in user's default profile.
    actor_username: str | None = None
    default_timezone: str = "UTC"
    on_removed: Literal["cancel", "delete", "ignore"] = "cancel"
    visibility: Literal["PUBLIC", "UNLISTED", "PRIVATE"] = "PUBLIC"
    category: str = "MEETING"
    tags: list[str] = field(default_factory=list)
    # Cap on how many upcoming events (nearest first) to sync in a run.
    # None or a non-positive value means "no cap".
    max_future_events: int | None = None
    # Optional default physical address applied to every synced event.
    default_location: Location | None = None
    # Optional mapping of iCal LOCATION text to a specific physical address.
    # Supports wildcards (*, ?, [...]). Example: {"*makerspace*": {...}}.
    # Values use the same fields as default_location.
    location_mapping: dict[str, Location] = field(default_factory=dict)
    # Optional metadata (key-value pairs) applied to every synced event.
    # Each entry should have "key", "value", "title", and "type" fields.
    metadata: list[dict] = field(default_factory=list)
    # Optional default image (filename from images folder) applied to every synced event.
    # The image will be uploaded to Mobilizon automatically.
    default_image: str | None = None
    # Optional mapping of event titles to image filenames.
    # Title must match precisely. Example: {"Open Space/Chaostreff": "chaostreff.jpg"}
    # Images will be uploaded to Mobilizon automatically.
    title_image_mapping: dict[str, str] = field(default_factory=dict)
    # Optional list of wildcard patterns for event titles that should be skipped.
    # Supports *, ?, [...]. Example: ["*internal*", "*secret*"]
    exclude_titles: list[str] = field(default_factory=list)
    # Optional mapping of event title patterns to metadata entries.
    # Matching is case-insensitive and supports wildcards (*, ?, [...]).
    # Example: {"Open Space*": [{"key": "group", "title": "Chaotikum", "value": "https://chaotikum.org", "type": "STRING"}]}
    title_metadata_mapping: dict[str, list[dict]] = field(default_factory=dict)
    # Optional mapping of organization IDs to URLs for external address mapping files.
    # External files should be JSON with the same structure as location_mapping.
    # Example: {"chaotikum": "https://example.com/addresses.json"}
    # Remote mappings are merged with local ones; remote wins on key conflicts.
    location_mapping_urls: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    instance_url: str
    app: AppInfo
    state_file: Path
    http_timeout_seconds: float
    organizations: list[OrgConfig]
    # Optional mapping of organization IDs to URLs for external address mapping files.
    # External files should be JSON with a "location_mapping" key.
    # Example: {"chaotikum": "https://example.com/addresses.json"}
    location_mapping_urls: dict[str, str] = field(default_factory=dict)

    @property
    def instance_base(self) -> str:
        return self.instance_url.rstrip("/")


def _parse_location(raw: dict | None) -> Location | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("default_location must be an object")
    lat = raw.get("latitude")
    lon = raw.get("longitude")
    max_participants = raw.get("max_participants")
    return Location(
        description=raw.get("description"),
        street=raw.get("street"),
        locality=raw.get("locality"),
        postal_code=raw.get("postal_code"),
        region=raw.get("region"),
        country=raw.get("country"),
        type=raw.get("type"),
        url=raw.get("url"),
        timezone=raw.get("timezone"),
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
        geom=raw.get("geom"),
        max_participants=int(max_participants) if max_participants is not None else None,
    )


def _parse_recurring_pattern(raw: dict) -> RecurringPattern:
    if not isinstance(raw, dict):
        raise ValueError("recurring.pattern must be an object")
    return RecurringPattern(
        type=raw.get("type", "monthly"),
        day_of_week=raw.get("day_of_week"),
        week=raw.get("week"),
        time=raw.get("time"),
    )


def _parse_recurring_config(raw: dict | None) -> RecurringConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("recurring must be an object")
    return RecurringConfig(
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        url=raw.get("url"),
        pattern=_parse_recurring_pattern(raw.get("pattern", {})),
    )


def _fetch_external_location_mapping(url: str, timeout: float) -> dict[str, Location]:
    """Fetch and parse external location mapping from a URL.
    
    The external JSON should have a "location_mapping" key containing
    address objects in the same format as the local location_mapping.
    """
    import httpx
    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        raise ValueError(f"Failed to fetch or parse external location mapping from {url}: {e}")
    
    if "location_mapping" not in data:
        raise ValueError(f"External location mapping from {url} missing 'location_mapping' key")
    
    raw_mapping = data["location_mapping"]
    if not isinstance(raw_mapping, dict):
        raise ValueError(f"External location_mapping from {url} must be an object")
    
    return {key: _parse_location(value) for key, value in raw_mapping.items()}


def load_config(path: Path) -> Config:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if "instance_url" not in raw:
        raise ValueError("config: 'instance_url' is required")
    if "organizations" not in raw or not isinstance(raw["organizations"], list):
        raise ValueError("config: 'organizations' must be a list")

    app_raw = raw.get("app") or {}
    app = AppInfo(
        name=app_raw.get("name", "mobilizon-event-sync"),
        website=app_raw.get("website"),
    )

    state_file = Path(raw.get("state_file", "state.sqlite"))
    if not state_file.is_absolute():
        state_file = path.parent / state_file

    # Parse global location_mapping_urls
    location_mapping_urls = dict(raw.get("location_mapping_urls", {}))
    http_timeout = float(raw.get("http_timeout_seconds", 30))

    orgs: list[OrgConfig] = []
    seen_ids: set[str] = set()
    for i, o in enumerate(raw["organizations"]):
        for key in ("id", "group"):
            if key not in o:
                raise ValueError(f"config: organizations[{i}] missing '{key}'")
        
        # Ensure at least one event source is provided
        has_ical_url = "ical_url" in o and o["ical_url"]
        has_ical_file = "ical_file" in o and o["ical_file"]
        has_recurring = "recurring" in o and o["recurring"]
        
        if not (has_ical_url or has_ical_file or has_recurring):
            raise ValueError(
                f"config: organizations[{i}] must have one of 'ical_url', 'ical_file', or 'recurring'"
            )
        
        if o["id"] in seen_ids:
            raise ValueError(f"config: duplicate organization id {o['id']!r}")
        seen_ids.add(o["id"])
        orgs.append(
            OrgConfig(
                id=o["id"],
                group=o["group"],
                ical_url=o.get("ical_url"),
                ical_file=o.get("ical_file"),
                recurring=_parse_recurring_config(o.get("recurring")),
                active=o.get("active", True),
                actor_username=o.get("actor_username") or None,
                default_timezone=o.get("default_timezone", "UTC"),
                on_removed=o.get("on_removed", "cancel"),
                visibility=o.get("visibility", "PUBLIC"),
                category=o.get("category", "MEETING"),
                tags=list(o.get("tags", [])),
                max_future_events=(
                    int(o["max_future_events"]) if o.get("max_future_events") is not None else None
                ),
                default_location=_parse_location(o.get("default_location")),
                location_mapping={
                    p: _parse_location(v)
                    for p, v in o.get("location_mapping", {}).items()
                },
                location_mapping_urls=dict(o.get("location_mapping_urls", {})),
                metadata=list(o.get("metadata", [])),
                default_image=o.get("default_image"),
                title_image_mapping=dict(o.get("title_image_mapping", {})),
                exclude_titles=list(o.get("exclude_titles", [])),
                title_metadata_mapping=dict(o.get("title_metadata_mapping", {})),
            )
        )

    # Merge external location mappings for each organization
    for org in orgs:
        # Check org-specific URLs first, then global URLs
        urls_to_fetch = list(org.location_mapping_urls.items())
        if org.id in location_mapping_urls:
            urls_to_fetch.append((org.id, location_mapping_urls[org.id]))
        
        if urls_to_fetch:
            merged_mapping = dict(org.location_mapping)
            for url_id, url in urls_to_fetch:
                try:
                    external_mapping = _fetch_external_location_mapping(url, http_timeout)
                    # Merge with remote winning on conflicts
                    merged_mapping.update(external_mapping)
                except ValueError as e:
                    # Log warning but continue with local mapping
                    import logging
                    logging.warning(f"Failed to load external location mapping for {org.id} from {url}: {e}")
            # Replace the frozen org with a new one with merged mapping
            org_index = orgs.index(org)
            orgs[org_index] = OrgConfig(
                id=org.id,
                group=org.group,
                ical_url=org.ical_url,
                ical_file=org.ical_file,
                recurring=org.recurring,
                active=org.active,
                actor_username=org.actor_username,
                default_timezone=org.default_timezone,
                on_removed=org.on_removed,
                visibility=org.visibility,
                category=org.category,
                tags=org.tags,
                max_future_events=org.max_future_events,
                default_location=org.default_location,
                location_mapping=merged_mapping,
                metadata=org.metadata,
                default_image=org.default_image,
                title_image_mapping=org.title_image_mapping,
                exclude_titles=org.exclude_titles,
                title_metadata_mapping=org.title_metadata_mapping,
                location_mapping_urls=org.location_mapping_urls,
            )

    return Config(
        instance_url=raw["instance_url"],
        app=app,
        state_file=state_file,
        http_timeout_seconds=http_timeout,
        organizations=orgs,
        location_mapping_urls=location_mapping_urls,
    )
