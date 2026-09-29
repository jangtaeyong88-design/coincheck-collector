"""UTC timestamps shared by public collection status records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


SCHEMA_VERSION = "1.0"


def utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("UTC timestamp required")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid UTC timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must be UTC")
    return instant.astimezone(UTC)


def iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
