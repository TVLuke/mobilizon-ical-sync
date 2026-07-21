"""Sync logic: diff iCal feed against Mobilizon and apply changes."""

from __future__ import annotations

import fnmatch
import hashlib
import json as _json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .config import OrgConfig
from .ical import ICalEvent
from .mobilizon import MobilizonClient
from .state import EventMapping, State

log = logging.getLogger(__name__)


URL_RE = re.compile(r"^https?://", re.IGNORECASE)


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    deleted: int = 0
    skipped: int = 0
    filtered: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "updated": self.updated,
            "cancelled": self.cancelled,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "filtered": self.filtered,
            "errors": self.errors,
        }


def _payload_hash(variables: dict) -> str:
    """Stable SHA-256 of the payload we'd send, so config edits also trigger updates."""
    blob = _json.dumps(variables, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _is_excluded(title: str | None, patterns: list[str]) -> bool:
    if not title:
        return False
    t = title.strip().lower()
    return any(fnmatch.fnmatch(t, p.strip().lower()) for p in patterns)


def _metadata_for_event(title: str | None, org: OrgConfig) -> list[dict]:
    """Combine org-wide metadata with per-title metadata matched by wildcard patterns."""
    metadata = list(org.metadata)
    if not title:
        return metadata
    t = title.strip().lower()
    for raw_pattern, entries in org.title_metadata_mapping.items():
        if fnmatch.fnmatch(t, raw_pattern.strip().lower()):
            for entry in entries:
                metadata.append(dict(entry))
    return metadata


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _description_with_location(ev: ICalEvent, has_physical_address: bool) -> str:
    """Include the iCal LOCATION line inside the description when there's no
    structured physical address on the event (so the venue info stays visible)."""
    parts: list[str] = []
    if (
        not has_physical_address
        and ev.location_name
        and not URL_RE.match(ev.location_name.strip())
    ):
        parts.append(f"📍 {ev.location_name.strip()}")
    if ev.description:
        parts.append(ev.description)
    return "\n\n".join(parts) if parts else ""


def _online_address(ev: ICalEvent) -> str | None:
    if ev.url and URL_RE.match(ev.url.strip()):
        return ev.url.strip()
    if ev.location_name and URL_RE.match(ev.location_name.strip()):
        return ev.location_name.strip()
    return None


def _physical_address(ev: ICalEvent, org: OrgConfig) -> tuple[dict | None, int | None]:
    raw = ev.location_name.strip() if ev.location_name else ""
    # Check per-organization location mapping first (only for non-URL locations).
    if raw and not URL_RE.match(raw):
        t = raw.lower()
        for pattern, mapped in org.location_mapping.items():
            if fnmatch.fnmatch(t, pattern.strip().lower()):
                log.info(
                    "org=%s location mapping: %r matched pattern %r",
                    org.id,
                    raw,
                    pattern,
                )
                payload = mapped.to_address_input() or {}
                # Let the iCal LOCATION be the visible label if the mapping
                # didn't specify a description.
                if mapped.description is None:
                    payload["description"] = raw
                return (payload or None, mapped.max_participants)
    if org.default_location is None:
        return (None, None)
    payload = org.default_location.to_address_input() or {}
    # Prefer the iCal LOCATION as the visible label if it's plain text
    # (not a URL). Falls back to the config's `description`.
    if raw and not URL_RE.match(raw):
        payload["description"] = raw
    return (payload or None, org.default_location.max_participants)


def _get_image_media_id(
    ev: ICalEvent,
    org: OrgConfig,
    client: MobilizonClient,
    config_dir: Path,
    state: State,
) -> str | None:
    """Get the image media UUID for an event based on title mapping or default.

    Title mapping keys support Unix-style wildcards (*, ?, [...]). Exact matches
    take precedence; the first matching wildcard pattern is used.
    """
    title = (ev.title or "").strip()
    image_filename: str | None = None
    matched_pattern: str | None = None

    lowered_title = title.lower()
    stripped_map = {p.strip(): (p, f) for p, f in org.title_image_mapping.items()}

    # Exact match (case- and whitespace-insensitive)
    if title in stripped_map:
        matched_pattern, image_filename = stripped_map[title]
    else:
        # Wildcard match: first pattern that matches the event title
        for raw_pattern, (orig_pattern, mapped_file) in stripped_map.items():
            if fnmatch.fnmatch(lowered_title, raw_pattern.lower()):
                matched_pattern = orig_pattern
                image_filename = mapped_file
                break
        if image_filename is None:
            image_filename = org.default_image

    if image_filename:
        log.info(
            "image mapping: title=%r matched_pattern=%r filename=%r",
            title,
            matched_pattern,
            image_filename,
        )

    if not image_filename:
        return None

    # Reuse a previously uploaded media UUID for this filename so we don't
    # create a new server-side media object on every run (storage bloat) and
    # so the payload hash stays stable when the image hasn't changed.
    cached = state.get_media_uuid(org.id, image_filename)
    if cached:
        return cached

    # Construct the full path to the image
    image_path = config_dir / "images" / image_filename
    if not image_path.exists():
        log.warning(f"Image file not found: {image_path}")
        return None

    try:
        # Upload the image to Mobilizon
        media = client.upload_media(str(image_path), image_filename)
        uuid = media["uuid"]
        state.save_media_uuid(org.id, image_filename, uuid)
        return uuid
    except Exception as e:
        log.error(f"Failed to upload image {image_filename}: {e}")
        return None


def _base_variables(ev: ICalEvent, org: OrgConfig, client: MobilizonClient, config_dir: Path, state: State) -> dict:
    physical, max_participants = _physical_address(ev, org)
    image_media_id = _get_image_media_id(ev, org, client, config_dir, state)
    metadata = _metadata_for_event(ev.title, org)
    variables = {
        "title": ev.title,
        "description": _description_with_location(ev, has_physical_address=physical is not None),
        "beginsOn": _iso(ev.begins_on),
        "endsOn": _iso(ev.ends_on) if ev.ends_on else None,
        "status": ev.status if ev.status in {"CONFIRMED", "TENTATIVE", "CANCELLED"} else "CONFIRMED",
        "visibility": org.visibility,
        "category": org.category,
        "tags": sorted(set(org.tags + ev.tags)),
        "onlineAddress": _online_address(ev),
        "options": {
            "timezone": ev.timezone_name or org.default_timezone,
            "commentModeration": "CLOSED",
        },
        "physicalAddress": physical,
        "metadata": metadata,
    }
    if max_participants is not None:
        variables["options"]["maximumAttendeeCapacity"] = max_participants
    if image_media_id:
        variables["picture"] = {"mediaUuid": image_media_id}
    return variables


def sync_org(
    org: OrgConfig,
    ical_events: Iterable[ICalEvent],
    client: MobilizonClient,
    state: State,
    config_dir: Path,
) -> SyncStats:
    stats = SyncStats()
    events_sorted = sorted(ical_events, key=lambda e: e.begins_on)
    all_feed_uids = {e.uid for e in events_sorted}
    if org.max_future_events and org.max_future_events > 0:
        events_sorted = events_sorted[: org.max_future_events]
    ical_by_uid: dict[str, ICalEvent] = {e.uid: e for e in events_sorted}
    mappings = state.get_mappings(org.id)

    log.info(
        "org=%s: %d iCal events (capped to %d), %d known mappings",
        org.id,
        len(all_feed_uids),
        len(ical_by_uid),
        len(mappings),
    )

    organizer_actor_id = client.resolve_organizer_actor_id(org.actor_username)
    group_id = client.fetch_group_id(org.group)
    log.info(
        "org=%s: organizer_actor_id=%s group_id=%s",
        org.id, organizer_actor_id, group_id,
    )

    # --- Create or update -----------------------------------------------
    for uid, ev in ical_by_uid.items():
        if _is_excluded(ev.title, org.exclude_titles):
            stats.filtered += 1
            log.info("org=%s filtered: %s", org.id, ev.title)
            continue
        try:
            mapping = mappings.get(uid)
            variables_no_ids = _base_variables(ev, org, client, config_dir, state)
            new_hash = _payload_hash(variables_no_ids)

            if mapping is None:
                variables = dict(variables_no_ids)
                variables.update({
                    "organizerActorId": organizer_actor_id,
                    "attributedToId": group_id,
                })
                created = client.create_event(variables)
                state.upsert_mapping(
                    org.id,
                    EventMapping(ical_uid=uid, event_uuid=created["uuid"], content_hash=new_hash),
                )
                stats.created += 1
                log.info("org=%s created: %s (%s)", org.id, ev.title, created["uuid"])
            elif mapping.content_hash != new_hash or _options_differ_from_server(client, mapping.event_uuid, variables_no_ids.get("options")):
                # We need the numeric ID for updateEvent; look it up via
                # existing group events. Cheaper: search once (below in delete
                # phase); here call by UUID.
                event_id = _resolve_event_id_by_uuid(client, mapping.event_uuid)
                if event_id is None:
                    log.warning(
                        "org=%s update failed: event uuid %s not on server; recreating",
                        org.id, mapping.event_uuid,
                    )
                    state.delete_mapping(org.id, uid)
                    variables = dict(variables_no_ids)
                    variables.update({
                        "organizerActorId": organizer_actor_id,
                        "attributedToId": group_id,
                    })
                    created = client.create_event(variables)
                    state.upsert_mapping(
                        org.id,
                        EventMapping(ical_uid=uid, event_uuid=created["uuid"], content_hash=new_hash),
                    )
                    stats.created += 1
                    continue

                # Reset-then-set workaround for two interacting Mobilizon bugs:
                #   1) updateEvent with picture: {mediaUuid} on an event that
                #      ALREADY has a picture crashes (Ecto on_replace: :update).
                #   2) updateEvent WITHOUT picture forces picture=nil server-side
                #      (Map.update(:picture, nil, ...) in api/events.ex), which
                #      clears the existing image.
                # So we first update every field except the picture (this also
                # clears the picture), then issue a second update that only sets
                # the picture (succeeds because the event now has no picture).
                # The media UUID is cached/reused so no new media is created.
                picture = variables_no_ids.get("picture")
                variables = dict(variables_no_ids)
                variables.pop("picture", None)
                variables["eventId"] = event_id
                client.update_event(variables)
                if picture:
                    try:
                        client.update_event({"eventId": event_id, "picture": picture, "options": variables.get("options")})
                    except Exception as pic_exc:  # noqa: BLE001
                        log.warning(
                            "org=%s could not set picture for '%s': %s",
                            org.id,
                            ev.title,
                            pic_exc,
                        )
                state.upsert_mapping(
                    org.id,
                    EventMapping(ical_uid=uid, event_uuid=mapping.event_uuid, content_hash=new_hash),
                )
                stats.updated += 1
                log.info("org=%s updated: %s (%s)", org.id, ev.title, mapping.event_uuid)
            else:
                stats.skipped += 1
        except Exception as e:  # noqa: BLE001
            stats.errors += 1
            log.exception("org=%s failed to sync uid=%s: %s", org.id, uid, e)

    # --- Handle removed events ------------------------------------------
    # Only mappings whose UID is missing from the *full* feed count as removed;
    # UIDs that were merely trimmed by max_future_events are left alone.
    removed_uids = set(mappings) - all_feed_uids
    if removed_uids and org.on_removed != "ignore":
        for uid in removed_uids:
            mapping = mappings[uid]
            try:
                event_id = _resolve_event_id_by_uuid(client, mapping.event_uuid)
                if event_id is None:
                    log.info("org=%s already gone: %s", org.id, mapping.event_uuid)
                    state.delete_mapping(org.id, uid)
                    continue

                if org.on_removed == "delete":
                    client.delete_event(event_id)
                    state.delete_mapping(org.id, uid)
                    stats.deleted += 1
                    log.info("org=%s deleted: %s", org.id, mapping.event_uuid)
                else:  # cancel
                    client.update_event({"eventId": event_id, "status": "CANCELLED"})
                    # Keep the mapping so we won't cancel repeatedly, but
                    # invalidate the hash so a re-appearance triggers update.
                    state.upsert_mapping(
                        org.id,
                        EventMapping(
                            ical_uid=uid,
                            event_uuid=mapping.event_uuid,
                            content_hash="__cancelled__",
                        ),
                    )
                    stats.cancelled += 1
                    log.info("org=%s cancelled: %s", org.id, mapping.event_uuid)
            except Exception as e:  # noqa: BLE001
                stats.errors += 1
                log.exception("org=%s failed to cancel/delete uid=%s: %s", org.id, uid, e)

    return stats


def _options_differ_from_server(client: MobilizonClient, event_uuid: str, config_options: dict | None) -> bool:
    """Check if server event options differ from config options."""
    if config_options is None:
        return False
    data = client.graphql(
        "query($uuid: UUID!) { event(uuid: $uuid) { options { timezone maximumAttendeeCapacity } } }",
        {"uuid": event_uuid},
    )
    ev = data.get("event")
    if not ev:
        return False
    server_opts = ev.get("options", {})
    # Compare only the fields we care about
    for key in ["timezone", "maximumAttendeeCapacity"]:
        if config_options.get(key) != server_opts.get(key):
            log.info(
                "org=server options differ: %s config=%s server=%s",
                key,
                config_options.get(key),
                server_opts.get(key),
            )
            return True
    return False


def _resolve_event_id_by_uuid(client: MobilizonClient, uuid: str) -> str | None:
    """Fetch the numeric event ID for a given UUID (needed for update/delete)."""
    data = client.graphql(
        "query($uuid: UUID!) { event(uuid: $uuid) { id uuid } }",
        {"uuid": uuid},
    )
    ev = data.get("event")
    return ev["id"] if ev else None
