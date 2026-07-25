"""Day-bucket boundaries for the Productivity page's time-series (spec: D9/D17,
``docs/brainstorms/2026-07-24-productivity-page-requirements.md``).

Bucket boundaries are fixed to a single SERVER-SIDE zone, ``America/Denver`` — never a
client-supplied offset. This keeps every user's chart identical (no per-browser day-boundary
drift) and keeps the server-side cache key bounded to ``(range, bucket)`` rather than growing
an unbounded per-offset dimension. The productivity HTTP route must therefore never read a
client timezone/offset query parameter and thread it in here; the build-validation gate
(``no-client-supplied-timezone``) enforces that at the grep level across ``knowledge/serve``.

Each day's local midnight is converted to UTC using THAT DAY's actual UTC offset (via
``zoneinfo``), so a range spanning a daylight-saving transition gets a different UTC start
before vs after the transition rather than one offset applied uniformly across the range.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

#: The one fixed zone every day bucket boundary is computed against. Not configurable per
#: request — see module docstring.
BUCKET_TIMEZONE = "America/Denver"

_ZONE = ZoneInfo(BUCKET_TIMEZONE)
_UTC = ZoneInfo("UTC")


def daily_buckets(start: date, end: date) -> dict[str, Any]:
    """Bucket every calendar day in ``[start, end]`` (inclusive) to its ``America/Denver``
    midnight, expressed as a UTC instant.

    Returns ``{"timezone": "America/Denver", "buckets": [{"date": "YYYY-MM-DD",
    "start_utc": "<ISO-8601 Z>"}, ...]}`` — the shape the productivity route's response embeds
    directly, so the timezone is always disclosed alongside the series.
    """
    if end < start:
        raise ValueError(f"end ({end}) must be >= start ({start})")
    buckets: list[dict[str, str]] = []
    day = start
    while day <= end:
        local_midnight = datetime(day.year, day.month, day.day, tzinfo=_ZONE)
        start_utc = local_midnight.astimezone(_UTC)
        buckets.append({
            "date": day.isoformat(),
            "start_utc": start_utc.isoformat().replace("+00:00", "Z"),
        })
        day += timedelta(days=1)
    return {"timezone": BUCKET_TIMEZONE, "buckets": buckets}
