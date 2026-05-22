"""
Decorator with exponential delay for transient errors.

Used in hot paths where the network/Koji may blink:
- koji CLI subprocess (resolver.KojiClient._run_koji_command)
- HTTP downloads of SRPMs (fetcher)
- submit commands (builder._run_koji)

IMPORTANT: retry must NOT hide programming bugs: we retry only on
exceptions/signals matched by the `transient_marker` function.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)

# Markers of transient errors (by substrings in str(exc) or in stdout/stderr).
_TRANSIENT_MARKERS = (
    "connection",
    "timeout",
    "timed out",
    "temporary failure",
    "temporarily unavailable",
    "service unavailable",
    "gateway",
    "bad gateway",
    "503",
    "502",
    "504",
    "ssl: ",
    "remote end closed",
    "network is unreachable",
    "no route to host",
)


def is_transient(exc_or_text) -> bool:
    """Check whether an error looks transient (deserves a retry)."""
    if exc_or_text is None:
        return False
    text = str(exc_or_text).lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def with_retry(
    attempts: int = 3,
    backoff: float = 2.0,
    initial_delay: float = 1.0,
    transient_check: Optional[Callable[[BaseException], bool]] = None,
):
    """
    Decorator: repeats the call up to `attempts` times with exponential backoff.

    Args:
        attempts: total number of attempts (including the first). 3 = 1 first + 2 retries.
        backoff: delay multiplier (1.0 → 2.0 → 4.0 …).
        initial_delay: first delay in seconds.
        transient_check: predicate that decides which exceptions are transient.
                          Default is is_transient.
    """
    if transient_check is None:
        transient_check = lambda exc: is_transient(exc)  # noqa: E731

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exc: Optional[BaseException] = None
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if attempt >= attempts:
                        raise
                    if not transient_check(exc):
                        raise
                    logger.warning(
                        "%s attempt %d/%d failed (%s: %s); waiting %.1fs",
                        fn.__name__,
                        attempt,
                        attempts,
                        type(exc).__name__,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= backoff
            # Unreachable, but for the type checker
            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator
