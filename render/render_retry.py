"""Retry policy shared by the console image's Express and Cookie handlers."""
import os
import re

import httpx


def _number(name, default, minimum, maximum, cast):
    try:
        value = cast(os.environ.get(name, default))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


RETRY_ENABLED = os.environ.get("RETRY_ENABLED", "true").lower() == "true"
DEFAULT_RETRY_MAX = _number("RETRY_MAX_ATTEMPTS", 11, 1, 51, int) - 1
DEFAULT_BACKOFF = _number("RETRY_INTERVAL_MS", 5000, 0, 120000, float) / 1000
MAX_BACKOFF = _number("RETRY_MAX_BACKOFF_MS", 5000, 0, 120000, float) / 1000
_DEFAULT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
try:
    RETRY_STATUS_CODES = {
        int(value.strip())
        for value in os.environ.get("RETRY_STATUS_CODES", "408,409,425,429,500,502,503,504").split(",")
        if value.strip()
    }
except ValueError:
    RETRY_STATUS_CODES = _DEFAULT_STATUSES
# Authentication failures and invalid requests require correction, not retries.
RETRY_STATUS_CODES = {value for value in RETRY_STATUS_CODES if 100 <= value <= 599} - {400, 401, 403, 404}
_STATUS_PATTERNS = (
    re.compile(r"\b(?:HTTP|RPC|status(?:\s+code)?)\s*[:=]?\s*(\d{3})\b", re.I),
    re.compile(r"[\"']code[\"']\s*:\s*(\d{3})\b", re.I),
    re.compile(r"^\s*(\d{3})\b"),
)
_RETRY_MARKERS = (
    "resource exhausted", "resource_exhausted", "too many requests", "rate limit",
    "quota", "overloaded", "try again later", "temporarily unavailable", "internal error",
    "timeout", "timed out", "connection reset", "connection refused", "connection closed",
    "connection aborted", "server disconnected", "all connection attempts failed",
)


def retry_delay(attempt, initial_delay):
    """Double the delay after each failure, with a fixed upper bound."""
    initial_delay = max(0.0, min(MAX_BACKOFF, float(initial_delay)))
    return min(MAX_BACKOFF, initial_delay * (2 ** min(max(0, attempt), 20)))


def is_retryable(error):
    if not RETRY_ENABLED:
        return False
    for value in (
        getattr(error, "status_code", None), getattr(error, "code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value in RETRY_STATUS_CODES
    if isinstance(error, httpx.TransportError):
        return True
    message = str(error)
    for pattern in _STATUS_PATTERNS:
        match = pattern.search(message)
        if match:
            return int(match.group(1)) in RETRY_STATUS_CODES
    return any(marker in message.lower() for marker in _RETRY_MARKERS)
