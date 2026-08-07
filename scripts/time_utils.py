#!/usr/bin/env python3
"""Shared India Standard Time helpers for the news monitor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SCHEDULE_HOURS = (0, 6, 12, 18)
SCHEDULE_MINUTE = 17


def as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def to_ist(value: datetime) -> datetime:
    return as_aware(value).astimezone(IST)


def now_ist() -> datetime:
    return datetime.now(IST)


def format_time_ist(value: datetime) -> str:
    return to_ist(value).strftime("%I:%M %p").lstrip("0")


def format_datetime_ist(value: datetime) -> str:
    return to_ist(value).strftime("%d %b %Y, %I:%M %p IST").replace(" 0", " ")


def format_email_date(value: datetime) -> str:
    return to_ist(value).strftime("%b %d, %Y")


def next_scheduled_ist(value: datetime | None = None) -> datetime:
    current = to_ist(value or now_ist())
    for hour in SCHEDULE_HOURS:
        candidate = current.replace(hour=hour, minute=SCHEDULE_MINUTE, second=0, microsecond=0)
        if candidate > current:
            return candidate
    tomorrow = current + timedelta(days=1)
    return tomorrow.replace(hour=SCHEDULE_HOURS[0], minute=SCHEDULE_MINUTE, second=0, microsecond=0)
