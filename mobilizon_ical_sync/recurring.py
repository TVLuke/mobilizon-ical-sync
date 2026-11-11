"""Generate recurring events for organizations without iCal feeds."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

from dateutil import tz

from .ical import ICalEvent


@dataclass(frozen=True)
class RecurringPattern:
    """Pattern for recurring events."""
    type: Literal["monthly", "weekly"]
    day_of_week: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    week: Literal["first", "second", "third", "fourth", "last"]
    time: str  # HH:MM format
    duration: float


@dataclass(frozen=True)
class RecurringConfig:
    """Configuration for a recurring event."""
    title: str
    description: str
    url: str | None
    pattern: RecurringPattern


def _get_nth_weekday_of_month(year: int, month: int, weekday: int, week: str) -> date:
    """Get the nth weekday of a given month.
    
    Args:
        year: Year
        month: Month (1-12)
        weekday: Weekday (0=Monday, 6=Sunday)
        week: "first", "second", "third", "fourth", or "last"
    
    Returns:
        The date of the nth weekday of the month
    """
    if week == "last":
        # Get the last occurrence of the weekday
        # Start from the last day of the month and work backwards
        if month == 12:
            last_day = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(year, month + 1, 1) - timedelta(days=1)
        
        # Work backwards to find the target weekday
        while last_day.weekday() != weekday:
            last_day -= timedelta(days=1)
        return last_day
    
    # For first, second, third, fourth
    week_num = {"first": 1, "second": 2, "third": 3, "fourth": 4}[week]
    
    # Find the first occurrence of the weekday
    first_day = date(year, month, 1)
    while first_day.weekday() != weekday:
        first_day += timedelta(days=1)
    
    # Add the appropriate number of weeks
    return first_day + timedelta(weeks=week_num - 1)


def _parse_time(time_str: str) -> time:
    """Parse HH:MM string to time object."""
    hour, minute = map(int, time_str.split(":"))
    return time(hour, minute)


def _weekday_from_string(day: str) -> int:
    """Convert weekday string to integer (0=Monday, 6=Sunday)."""
    mapping = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    return mapping[day.lower()]


def generate_recurring_events(
    config: RecurringConfig,
    default_timezone: str,
    max_events: int,
    start_date: date | None = None,
) -> list[ICalEvent]:
    """Generate recurring events based on a pattern.
    
    Args:
        config: Recurring event configuration
        default_timezone: Timezone name for the events
        max_events: Maximum number of future events to generate
        start_date: Date to start generating from (defaults to today)
    
    Returns:
        List of ICalEvent objects
    """
    if start_date is None:
        start_date = date.today()
    
    timezone_obj = tz.gettz(default_timezone) or tz.UTC
    weekday = _weekday_from_string(config.pattern.day_of_week)
    time_obj = _parse_time(config.pattern.time)
    length = config.pattern.duration or 2
    recurrance = config.pattern.type
    
    events: list[ICalEvent] = []
    current_date = start_date
    
    # Generate events month by month
    while len(events) < max_events:
        if recurrance == 'monthly':
            # Get the target date for this month
            event_date = _get_nth_weekday_of_month(
                current_date.year, current_date.month, weekday, config.pattern.week
            )
            # Skip if the date is before our start date
            if event_date < start_date:
                current_date = _get_next_month(current_date)
                continue
        elif recurrance == 'weekly':
            # get the day of the week
            # does not have an increment so far
            days_until = (weekday - current_date.weekday() +7) % 7
            event_date = current_date + timedelta(days=days_until)
        
        
        # Create the datetime
        event_datetime = datetime.combine(event_date, time_obj, tzinfo=timezone_obj)
        
        # Only include future events
        if event_datetime > datetime.now(timezone_obj):
            # Generate a unique UID based on the date
            uid = f"recurring-{config.title.lower().replace(' ', '-')}-{event_date.isoformat()}"
            
            # Calculate end time (2 hours after start by default)
            end_datetime = event_datetime + timedelta(hours=length)
            
            events.append(
                ICalEvent(
                    uid=uid,
                    title=config.title,
                    description=config.description,
                    begins_on=event_datetime,
                    ends_on=end_datetime,
                    timezone_name=default_timezone,
                    location_name=None,
                    location_address=None,
                    url=config.url,
                    status="CONFIRMED",
                    all_day=False,
                    tags=[],
                )
            )
        
        if recurrance == 'monthly':
            # Move to next month
            current_date = _get_next_month(current_date)
        else:
            # move to next week
            current_date = current_date + timedelta(days=7)
    
    return events


def _get_next_month(d: date) -> date:
    """Get the first day of the next month."""
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)
