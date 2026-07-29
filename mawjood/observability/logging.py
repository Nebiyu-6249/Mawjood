"""Structured JSON logging.

Every log line is JSON by default so the whole pipeline — conversations, API
calls, routing decisions — is queryable with a 90-day retention policy behind it.

Two things happen here that are compliance requirements, not conveniences:

* **Credential redaction.** Values under credential-ish keys are replaced before
  a record can reach a handler. Adapters receive resolved credentials at call
  time (see CLAUDE.md section 4) and those must never land in a log.
* **PII minimisation.** Phone numbers and similar identifiers are masked to their
  last three characters. Enough to correlate a conversation, not enough to be a
  consumer contact list sitting in log storage.

stdlib logging is routed through the same processor chain, so uvicorn, SQLAlchemy
and third-party libraries emit the same JSON shape rather than plain text.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from typing import Any, Final

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp
from structlog.types import EventDict, Processor

REDACTED: Final = "***redacted***"
_MAX_REDACT_DEPTH: Final = 6

# Substring match against the key name, lowercased.
_SECRET_KEY_PATTERN: Final = re.compile(
    r"(password|passwd|secret|token|api[-_]?key|apikey|authorization|auth[-_]?header"
    r"|credential|private[-_]?key|access[-_]?key|signature|cookie|dsn|session[-_]?key)"
)
_PII_KEY_PATTERN: Final = re.compile(r"(phone|msisdn|whatsapp[-_]?id|wa[-_]?id|email)")


def _mask_pii(value: str) -> str:
    """Keep the last three characters so a human can still correlate a thread."""
    if len(value) <= 3:
        return "*" * len(value)
    return "*" * (len(value) - 3) + value[-3:]


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth >= _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _scrub_pair(str(k), v, depth) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub(item, depth + 1) for item in value]
        return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def _scrub_pair(key: str, value: Any, depth: int) -> Any:
    lowered = key.lower()
    if _SECRET_KEY_PATTERN.search(lowered):
        return REDACTED
    if _PII_KEY_PATTERN.search(lowered) and isinstance(value, str):
        return _mask_pii(value)
    return _scrub(value, depth + 1)


def redact_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Strip credentials and mask PII before anything reaches a handler."""
    result: EventDict = {}
    for key, value in event_dict.items():
        result[key] = _scrub_pair(str(key), value, 0)
    return result


def _service_context(service_name: str, environment: str, region: str) -> Processor:
    def processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service_name)
        event_dict.setdefault("env", environment)
        event_dict.setdefault("region", region)
        return event_dict

    return processor


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "json",
    service_name: str = "mawjood",
    environment: str = "local",
    region: str = "me-central-1",
) -> None:
    """Configure structlog and route stdlib logging through the same chain."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _service_context(service_name, environment, region),
        redact_processor,
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Processor
    if log_format == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
        exc_processor: Processor = structlog.processors.format_exc_info
    else:
        renderer = structlog.processors.JSONRenderer()
        exc_processor = structlog.processors.dict_tracebacks

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            exc_processor,
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Uvicorn installs its own handlers; make them defer to ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers = []
        stdlib_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


class RequestContextMiddleware:
    """Bind a request id to the log context and record every request's outcome.

    Pure ASGI rather than BaseHTTPMiddleware so it does not buffer the response
    body — the request path stays streaming and async end to end.
    """

    def __init__(self, app: ASGIApp, header_name: str = "x-request-id") -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        incoming = request.headers.get(self.header_name)
        request_id = incoming or uuid.uuid4().hex
        started = time.perf_counter()
        status_code = 500

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        async def send_wrapper(message: Any) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = message.setdefault("headers", [])
                headers.append((self.header_name.encode(), request_id.encode()))
            await send(message)

        log = get_logger(__name__)
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception(
                "request.failed",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        else:
            log.info(
                "request.completed",
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        finally:
            structlog.contextvars.clear_contextvars()


__all__ = [
    "REDACTED",
    "RequestContextMiddleware",
    "configure_logging",
    "get_logger",
    "redact_processor",
]
