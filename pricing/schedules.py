"""Versioned UTC billing schedules; no network access or implicit calendar guesses."""

from datetime import date
from itertools import pairwise
from typing import Any, Mapping

from pricing.models import PricingUnavailableError, utc, validate_rates


def validate_schedule(value: Mapping[str, Any], tokens: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"timezone", "peak_weekdays", "peak_windows", "holiday_calendar", "off_peak_tokens"}
    if not isinstance(value, Mapping) or set(value) != expected or value["timezone"] != "UTC":
        raise ValueError("Unsupported billing schedule")
    weekdays = value["peak_weekdays"]
    if (
        not isinstance(weekdays, list)
        or not weekdays
        or any(type(day) is not int or day not in range(7) for day in weekdays)
        or len(set(weekdays)) != len(weekdays)
    ):
        raise ValueError("Invalid peak weekdays")
    windows = value["peak_windows"]
    if not isinstance(windows, list) or not windows:
        raise ValueError("Missing peak windows")
    normalized_windows = []
    for window in windows:
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("Invalid peak window")
        minutes = []
        for clock in window:
            if not isinstance(clock, str) or len(clock) != 5 or clock[2] != ":":
                raise ValueError("Invalid UTC clock")
            hour, minute = int(clock[:2]), int(clock[3:])
            if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and minute):
                raise ValueError("Invalid UTC clock")
            minutes.append(hour * 60 + minute)
        if minutes[0] >= minutes[1]:
            raise ValueError("Peak window must not cross midnight")
        normalized_windows.append((minutes, window))
    normalized_windows.sort()
    if any(a[0][1] > b[0][0] for a, b in pairwise(normalized_windows)):
        raise ValueError("Overlapping peak windows")
    calendar = value["holiday_calendar"]
    if not isinstance(calendar, Mapping) or set(calendar) != {"year", "ranges", "source_url"}:
        raise ValueError("Explicit verified holiday calendar required")
    year = calendar["year"]
    if (
        type(year) is not int
        or not 2000 <= year <= 9998
        or not str(calendar["source_url"]).startswith("https://")
    ):
        raise ValueError("Invalid holiday calendar year/source")
    if not isinstance(calendar["ranges"], list):
        raise ValueError("Invalid holiday ranges")
    ranges = []
    for span in calendar["ranges"]:
        if not isinstance(span, list) or len(span) != 2:
            raise ValueError("Invalid holiday range")
        start, end = (date.fromisoformat(item) for item in span)
        if start.year != year or end.year != year or start > end:
            raise ValueError("Holiday range outside calendar year")
        ranges.append([start.isoformat(), end.isoformat()])
    ranges.sort()
    if any(a[1] >= b[0] for a, b in pairwise(ranges)):
        raise ValueError("Overlapping holiday ranges")
    off_peak = validate_rates({"tokens": value["off_peak_tokens"]})["tokens"]
    if set(off_peak) != set(tokens):
        raise ValueError("Schedule token dimensions must match peak rates")
    return {
        "timezone": "UTC",
        "peak_weekdays": sorted(weekdays),
        "peak_windows": [window for _, window in normalized_windows],
        "holiday_calendar": {"year": year, "ranges": ranges, "source_url": calendar["source_url"]},
        "off_peak_tokens": off_peak,
    }


def scheduled_tokens(schedule: Mapping[str, Any], at: Any) -> tuple[dict[str, Any], str]:
    timestamp = utc(at)
    calendar = schedule["holiday_calendar"]
    if timestamp.year != calendar["year"]:
        raise PricingUnavailableError(
            "Verified billing holiday calendar does not cover request year"
        )
    day = timestamp.date().isoformat()
    holiday = any(start <= day <= end for start, end in calendar["ranges"])
    minute = timestamp.hour * 60 + timestamp.minute
    peak = (
        not holiday
        and timestamp.weekday() in schedule["peak_weekdays"]
        and any(
            int(start[:2]) * 60 + int(start[3:]) <= minute < int(end[:2]) * 60 + int(end[3:])
            for start, end in schedule["peak_windows"]
        )
    )
    return ({}, "peak") if peak else (dict(schedule["off_peak_tokens"]), "off_peak")
