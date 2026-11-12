from __future__ import annotations

import logging.config
import sys

from uvicorn.config import LOGGING_CONFIG

from vortex.stdstreams import ensure_standard_streams


def test_ensure_standard_streams_fills_missing(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    ensure_standard_streams()

    assert sys.stdout is not None
    assert sys.stderr is not None
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": LOGGING_CONFIG["formatters"],
        }
    )
