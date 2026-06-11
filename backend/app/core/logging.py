"""Structured logging: JSON in production, pretty console in development."""

from __future__ import annotations

import logging
import sys

import structlog

from app.core.config import Settings

_configured = False


def configure(settings: Settings) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")

    renderer: structlog.typing.Processor
    if settings.is_prod:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
