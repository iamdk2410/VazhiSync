"""structlog setup: JSON logs to stdout. Every service calls `configure_logging` once at start."""

import logging
import sys

import structlog


def configure_logging(service: str, level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # carries run_id once B10 binds it
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(),
        # Not cached: log capture in tests (and reconfiguration) must always take effect;
        # at this app's log rate the cache saves nothing measurable.
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.bind_contextvars(service=service)
