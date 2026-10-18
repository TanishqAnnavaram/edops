"""JSON logging. Every line carries correlation_id and order_id (null when not known)."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            # extra wins: the relay logs many rows, each with its own correlation id, in one loop.
            "correlation_id": getattr(record, "correlation_id", None) or correlation_id_var.get(),
            "order_id": getattr(record, "order_id", None),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Route uvicorn through the JSON handler. Its access log runs outside the request context
    # (no correlation id), so the middleware logs each request instead.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # A crashing worker would otherwise print a plain-text traceback, the one non-JSON line.
    sys.excepthook = _log_uncaught


def _log_uncaught(exc_type, exc, tb) -> None:
    logging.getLogger("app").critical("uncaught exception, exiting", exc_info=(exc_type, exc, tb))
