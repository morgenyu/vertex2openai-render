"""Shared retry helpers for transient upstream API failures."""

import asyncio
import inspect
import re
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, TypeVar, Union

import config as app_config


T = TypeVar("T")
AsyncOperation = Callable[[], Awaitable[T]]
AsyncIteratorFactory = Callable[
    [],
    Union[AsyncIterator[T], Awaitable[AsyncIterator[T]]],
]


_STATUS_PATTERNS = (
    re.compile(r'\bstatus(?:\s+code)?\s*[:=]?\s*(\d{3})\b', re.IGNORECASE),
    re.compile(r'["\']code["\']\s*:\s*(\d{3})\b', re.IGNORECASE),
    re.compile(r'\b(?:HTTP|RPC)\s+(\d{3})\b', re.IGNORECASE),
)

_TRANSIENT_MESSAGE_MARKERS = (
    "rate limit",
    "resource exhausted",
    "too many requests",
    "temporarily unavailable",
    "service unavailable",
    "server overloaded",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection closed",
    "timed out",
    "timeout",
    "try again",
)

_TRANSIENT_EXCEPTION_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "ConnectionError",
    "PoolTimeout",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "TimeoutError",
    "TimeoutException",
    "WriteError",
    "WriteTimeout",
}


def _coerce_status_code(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, int):
        return enum_value
    try:
        text_value = str(value).strip()
        if text_value.isdigit():
            return int(text_value)
    except Exception:
        pass
    return None


def get_exception_status_code(exc: BaseException) -> Optional[int]:
    """Best-effort extraction for google-genai, OpenAI, httpx and aiohttp errors."""
    pending = [exc]
    visited = set()

    while pending:
        current = pending.pop(0)
        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)

        for attr_name in ("status_code", "status", "code"):
            status_code = _coerce_status_code(getattr(current, attr_name, None))
            if status_code is not None:
                return status_code

        response = getattr(current, "response", None)
        if response is not None:
            for attr_name in ("status_code", "status"):
                status_code = _coerce_status_code(getattr(response, attr_name, None))
                if status_code is not None:
                    return status_code

        message = str(current)
        for pattern in _STATUS_PATTERNS:
            match = pattern.search(message)
            if match:
                return int(match.group(1))

        for chained in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(chained, BaseException):
                pending.append(chained)

    return None


def is_retryable_exception(exc: BaseException) -> bool:
    """Return True for configured HTTP statuses and common transient network errors."""
    status_code = get_exception_status_code(exc)
    if status_code is not None:
        return status_code in app_config.RETRY_STATUS_CODES

    exception_names = {cls.__name__ for cls in type(exc).__mro__}
    if exception_names.intersection(_TRANSIENT_EXCEPTION_NAMES):
        return True

    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_MESSAGE_MARKERS)


def _retry_settings(
    max_attempts: Optional[int],
    interval_ms: Optional[int],
) -> tuple[int, float]:
    attempts = app_config.RETRY_MAX_ATTEMPTS if max_attempts is None else max(1, max_attempts)
    delay_ms = app_config.RETRY_INTERVAL_MS if interval_ms is None else max(0, interval_ms)
    return attempts, delay_ms / 1000.0


async def retry_async(
    operation: AsyncOperation[T],
    operation_name: str,
    *,
    max_attempts: Optional[int] = None,
    interval_ms: Optional[int] = None,
    retry_predicate: Callable[[BaseException], bool] = is_retryable_exception,
) -> T:
    """Run an async operation with fixed-delay retries for transient failures."""
    attempts, delay_seconds = _retry_settings(max_attempts, interval_ms)
    if not app_config.RETRY_ENABLED:
        attempts = 1

    for attempt_number in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            should_retry = retry_predicate(exc)
            if not should_retry or attempt_number >= attempts:
                raise

            status_code = get_exception_status_code(exc)
            status_text = str(status_code) if status_code is not None else type(exc).__name__
            print(
                f"WARNING: {operation_name} failed with {status_text}; "
                f"retrying after {delay_seconds:.3f}s "
                f"(attempt {attempt_number + 1}/{attempts})."
            )
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

    raise RuntimeError(f"Retry loop for {operation_name} exited unexpectedly.")


async def async_iter_with_retry(
    iterator_factory: AsyncIteratorFactory[T],
    operation_name: str,
    *,
    max_attempts: Optional[int] = None,
    interval_ms: Optional[int] = None,
    retry_predicate: Callable[[BaseException], bool] = is_retryable_exception,
) -> AsyncIterator[T]:
    """
    Retry creation/consumption of an async stream until its first item is received.

    Once an upstream item has been received, retrying could duplicate output, so any
    later failure is propagated immediately.
    """
    attempts, delay_seconds = _retry_settings(max_attempts, interval_ms)
    if not app_config.RETRY_ENABLED:
        attempts = 1

    for attempt_number in range(1, attempts + 1):
        received_upstream_item = False
        try:
            iterator_or_awaitable = iterator_factory()
            if inspect.isawaitable(iterator_or_awaitable):
                iterator = await iterator_or_awaitable
            else:
                iterator = iterator_or_awaitable

            async for item in iterator:
                received_upstream_item = True
                yield item
            return
        except Exception as exc:
            should_retry = not received_upstream_item and retry_predicate(exc)
            if not should_retry or attempt_number >= attempts:
                raise

            status_code = get_exception_status_code(exc)
            status_text = str(status_code) if status_code is not None else type(exc).__name__
            print(
                f"WARNING: {operation_name} failed before the first stream item "
                f"with {status_text}; retrying after {delay_seconds:.3f}s "
                f"(attempt {attempt_number + 1}/{attempts})."
            )
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
