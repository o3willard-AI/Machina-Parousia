"""Structured JSON logging setup for Parousia Guard."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional


class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines for syslog/structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])
        return json.dumps(log_entry, default=str)


def setup_logging(
    level: str = "info",
    output: str = "stdout",
    log_format: str = "json",
) -> logging.Logger:
    """Configure structured logging for parousia-guard.

    Args:
        level: Log level (debug, info, warning, error).
        output: "stdout" for console, "syslog" for system logger.
        log_format: "json" for structured JSON, "text" for human-readable.

    Returns:
        Root logger for the parousia package.
    """
    logger = logging.getLogger("parousia")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    if log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

    if output == "syslog":
        try:
            handler: logging.Handler = logging.handlers.SysLogHandler(  # type: ignore[attr-defined]
                address="/dev/log"
            )
        except AttributeError:
            handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.StreamHandler(sys.stdout)

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Get a named logger under the parousia namespace."""
    full_name = f"parousia.{name}" if name else "parousia"
    return logging.getLogger(full_name)
