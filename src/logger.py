"""
Structured logging subsystem for ReliaDL using structlog.
Provides JSON formatting for machine ingestion, colored text for interactive CLI,
comprehensive credential/header redaction, and optional file logging.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional, Union

try:
    import structlog
    from structlog.types import EventDict, Processor
    _HAS_STRUCTLOG = True
except ImportError:
    _HAS_STRUCTLOG = False

# Sensitive key names to automatically redact (case-insensitive)
_SENSITIVE_KEYS = {
    "authorization",
    "proxy-authorization",
    "proxy_authorization",
    "x-api-key",
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "access_token",
    "auth_token",
    "bearer",
    "cookie",
    "set-cookie",
}

# Regex to sanitize credentials embedded in URLs: http://user:pass@host
_URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)([^:]+):([^@]+)@", re.IGNORECASE)

# Regex to sanitize Bearer tokens in string payloads
_BEARER_TOKEN_PATTERN = re.compile(r"Bearer\s+([A-Za-z0-9\-_\.=]+)", re.IGNORECASE)


def redact_credentials(val: Any) -> Any:
    """
    Recursively sanitize sensitive tokens, passwords, and authorization headers.
    Walks dictionaries, lists, and string payloads to prevent credential leakage.
    """
    if isinstance(val, dict):
        sanitized_dict: dict[str, Any] = {}
        for k, v in val.items():
            k_lower = str(k).lower().strip()
            if k_lower in _SENSITIVE_KEYS or any(sens in k_lower for sens in ("password", "secret", "token")):
                sanitized_dict[k] = "[REDACTED]"
            else:
                sanitized_dict[k] = redact_credentials(v)
        return sanitized_dict

    if isinstance(val, list):
        return [redact_credentials(item) for item in val]

    if isinstance(val, tuple):
        return tuple(redact_credentials(item) for item in val)

    if isinstance(val, str):
        cleaned = _URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]:[REDACTED]@", val)
        cleaned = _BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED]", cleaned)
        return cleaned

    return val


def redact_credentials_processor(
    logger: Any,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """
    Custom structlog processor that automatically scrubs sensitive credentials
    before formatting or output emission.
    """
    return redact_credentials(event_dict)


def get_log_level_value(level_str: str) -> int:
    """Convert string log level name to logging level integer."""
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    return mapping.get(level_str.strip().upper(), logging.INFO)


def configure_logger(
    level: str = "INFO",
    format_type: str = "json",
    log_file: Optional[Union[str, Path]] = None,
) -> None:
    """
    Configure global structured logging pipeline.

    Args:
        level: Log verbosity ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
        format_type: Output formatting ('json' or 'text')
        log_file: Optional path for destination log file
    """
    log_level = get_log_level_value(level)

    # Configure root standard library logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    for h in list(root_logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        root_logger.removeHandler(h)

    # Stream Handler (stdout / stderr)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(log_level)
    root_logger.addHandler(stream_handler)

    # Optional File Handler
    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(p, encoding="utf-8")
        file_handler.setLevel(log_level)
        root_logger.addHandler(file_handler)

    if _HAS_STRUCTLOG:
        shared_processors: list[Processor] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_credentials_processor,
        ]

        if format_type.lower() == "text":
            renderer: Processor = structlog.dev.ConsoleRenderer(colors=False)
        else:
            renderer = structlog.processors.JSONRenderer()

        structlog.configure(
            processors=shared_processors + [
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
        stream_handler.setFormatter(formatter)
        if log_file:
            file_handler.setFormatter(formatter)


def get_logger(name: Optional[str] = None) -> Any:
    """
    Retrieve a configured structured logger instance.
    """
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)
