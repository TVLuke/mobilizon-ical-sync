"""Fetch and normalize iCal feeds."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from typing import Iterable

import httpx
from dateutil import tz
from icalendar import Calendar


@dataclass(frozen=True)
class ICalEvent:
    uid: str
    title: str
    description: str
    begins_on: datetime  # timezone-aware
    ends_on: datetime | None
    timezone_name: str
    location_name: str | None
    location_address: str | None
    url: str | None
    status: str  # CONFIRMED / TENTATIVE / CANCELLED
    all_day: bool
    tags: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        payload = {
            "title": self.title,
            "description": self.description,
            "begins_on": self.begins_on.isoformat(),
            "ends_on": self.ends_on.isoformat() if self.ends_on else None,
            "timezone_name": self.timezone_name,
            "location_name": self.location_name,
            "location_address": self.location_address,
            "url": self.url,
            "status": self.status,
            "all_day": self.all_day,
            "tags": sorted(self.tags),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def as_dict(self) -> dict:
        d = asdict(self)
        d["begins_on"] = self.begins_on.isoformat()
        d["ends_on"] = self.ends_on.isoformat() if self.ends_on else None
        return d


def fetch_ical(url: str, timeout: float) -> str:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url, headers={"Accept": "text/calendar, */*;q=0.1"})
        r.raise_for_status()
        return r.text


def _to_aware(dt_value, default_tz_name: str) -> tuple[datetime, str, bool]:
    """Return (aware datetime, tz name, all_day)."""
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            zone = tz.gettz(default_tz_name) or timezone.utc
            dt_value = dt_value.replace(tzinfo=zone)
        tz_name = _tz_name(dt_value.tzinfo, default_tz_name)
        return dt_value, tz_name, False
    if isinstance(dt_value, date):
        zone = tz.gettz(default_tz_name) or timezone.utc
        dt = datetime.combine(dt_value, time(0, 0), tzinfo=zone)
        return dt, default_tz_name, True
    raise TypeError(f"Unsupported iCal date value: {dt_value!r}")


def _tz_name(tzinfo, fallback: str) -> str:
    name = getattr(tzinfo, "zone", None) or getattr(tzinfo, "key", None)
    if name:
        return str(name)
    if tzinfo == timezone.utc:
        return "UTC"
    return fallback


def parse_ical(text: str, default_timezone: str) -> list[ICalEvent]:
    cal = Calendar.from_ical(text)
    events: list[ICalEvent] = []
    now = datetime.now(timezone.utc)

    for comp in cal.walk("VEVENT"):
        # Skip recurrence instances that are declined/cancelled? We keep CANCELLED
        # so the sync can propagate cancellations.
        dtstart_prop = comp.get("DTSTART")
        if dtstart_prop is None:
            continue
        begins_on, tz_name, all_day = _to_aware(dtstart_prop.dt, default_timezone)

        # Only future events (based on end when available, else start).
        dtend_prop = comp.get("DTEND")
        ends_on: datetime | None = None
        if dtend_prop is not None:
            ends_on, _, _ = _to_aware(dtend_prop.dt, default_timezone)
        elif comp.get("DURATION") is not None:
            ends_on = begins_on + comp.decoded("DURATION")

        reference = ends_on or begins_on
        if reference < now:
            continue

        uid = str(comp.get("UID") or "").strip()
        if not uid:
            # Skip untrackable events
            continue

        status = str(comp.get("STATUS") or "CONFIRMED").upper()

        location_prop = comp.get("LOCATION")
        location_name = str(location_prop) if location_prop else None

        categories = comp.get("CATEGORIES")
        tags: list[str] = []
        if categories is not None:
            if hasattr(categories, "cats"):
                tags = [str(c) for c in categories.cats]
            else:
                tags = [t.strip() for t in str(categories).split(",") if t.strip()]

        url_prop = comp.get("URL")
        url_value = str(url_prop) if url_prop else None

        title = str(comp.get("SUMMARY") or "").strip() or "(untitled)"
        description = str(comp.get("DESCRIPTION") or "").strip()

        events.append(
            ICalEvent(
                uid=uid,
                title=title,
                description=description,
                begins_on=begins_on,
                ends_on=ends_on,
                timezone_name=tz_name,
                location_name=location_name,
                location_address=location_name,  # iCal LOCATION is a single free-text field
                url=url_value,
                status=status,
                all_day=all_day,
                tags=tags,
            )
        )
    return events


def load_ical(url: str, timeout: float, default_timezone: str) -> list[ICalEvent]:
    text = fetch_ical(url, timeout)
    return parse_ical(text, default_timezone)


def load_ical_file(file_path: str, default_timezone: str) -> list[ICalEvent]:
    """Load iCal from a local file instead of a URL."""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return parse_ical(text, default_timezone)


def dedupe(events: Iterable[ICalEvent]) -> list[ICalEvent]:
    """Keep last occurrence per UID (handles simple RECURRENCE-ID overrides naively)."""
    by_uid: dict[str, ICalEvent] = {}
    for e in events:
        by_uid[e.uid] = e
    return list(by_uid.values())
