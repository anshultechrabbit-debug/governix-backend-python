import json
import logging
import re
import sys
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.request_context import get_request_id

_SECRET_PATTERNS = [
    # Must run before the key=value rule, which would otherwise mask only "Bearer".
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    # scheme://user:password@host  ->  scheme://user:***@host
    (re.compile(r"(\w+://[^:/\s]+:)[^@\s]+(@)"), r"\1***\2"),
    # key=value / "key": "value" for sensitive keys
    (
        re.compile(
            r"(?i)((?:password|passwd|secret|token|api[_-]?key|authorization|access[_-]?key)"
            r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)"
        ),
        r"\1***",
    ),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-***"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if settings.LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            RedactingFormatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.LOG_LEVEL.upper())
