"""Tiny cross-cutting helpers."""

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Current UTC time as a second-precision ISO 8601 string (the state format)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()
