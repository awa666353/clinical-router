"""Log redaction (W8.5).

Patient tokens, access tokens and bearer credentials must never reach logs.
Uvicorn's access log is disabled in production (see Dockerfile); this filter is
defence in depth for application and development logs.
"""
from __future__ import annotations

import logging
import re

_PATTERNS = (
    re.compile(r"(/api/patient/session/)[^/\s?]+"),
    re.compile(r"([?&]token=)[^&\s]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(laya_(?:access|refresh)_token=)[^;\s]+"),
)


def redact(value: str) -> str:
    for pattern in _PATTERNS:
        value = pattern.sub(r"\1<redacted>", value)
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
            elif isinstance(record.args, dict):
                record.args = {
                    key: redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
        except Exception:  # pragma: no cover - logging must never raise
            return True
        return True


def install_log_redaction() -> None:
    redacting_filter = RedactingFilter()
    targets = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
    ]
    for logger in targets:
        if not any(isinstance(existing, RedactingFilter) for existing in logger.filters):
            logger.addFilter(redacting_filter)
        for handler in logger.handlers:
            if not any(isinstance(existing, RedactingFilter) for existing in handler.filters):
                handler.addFilter(redacting_filter)
