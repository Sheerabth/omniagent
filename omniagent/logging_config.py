"""Structured JSON logging shared by the control plane and worker.

Correlates logs via a `trace_id` contextvar: the API sets it to a per-request
id, the worker sets it to the session id, so grepping one id surfaces the
whole story across both processes.
"""

import json
import logging
from contextvars import ContextVar

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": trace_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _SuppressRawWsFilter(logging.Filter):
    """Drop google-antigravity RAW WS MSG logs — they echo full sandbox output
    including env vars, secrets, and command results."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name == "root" and "RAW WS MSG" in record.getMessage())


def configure_logging() -> None:
    from omniagent.config import settings

    level = settings.log_level.upper()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_SuppressRawWsFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
