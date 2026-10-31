"""Pickup-date rules shared by storage, tools and UI; no import-time I/O."""
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


BUSINESS_TIMEZONE = "Asia/Kolkata"


def business_today():
    return datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()


def validate_pickup_date(value, *, allow_past=False):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Choose a pickup date in YYYY-MM-DD format.")
    try:
        chosen = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Choose a valid calendar pickup date.") from None
    if not allow_past and chosen < business_today():
        raise ValueError("The pickup date cannot be in the past (India time).")
    return chosen.isoformat()
