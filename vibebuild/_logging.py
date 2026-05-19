"""
JSON log formatter for production parsing (rsyslog/loki/elastic).

Usage:
    setup_logging(verbose=True, json_logs=True)

Any `logger.info("msg", extra={"package": "X"})` will be serialized as JSON
with timestamp/level/module/message/package fields.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

# LogRecord attributes that we do NOT copy into JSON (everything in the standard set).
_STANDARD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
        "taskName",  # Python 3.12+
    }
)


class JsonFormatter(logging.Formatter):
    """Formatter that encodes a LogRecord as a one-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        # Custom fields from logger.info(..., extra={...})
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)
