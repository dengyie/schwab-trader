"""Small shared helpers for the market feeds.

Kept separate from both feed modules so neither direction of the
US-feed/intl-feed relationship grows an import cycle over a utility.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable

logger = logging.getLogger(__name__)

# Listener failures used to vanish into bare `except: pass` — the quote
# listener is broker.on_quote (matching engine + ledger write), so a broken
# fill path looked green while orders silently stopped executing
# (2026-11-23 review P3-2). Throttled per label: a failing listener sits on
# the tick path and could otherwise flood the log at stream rate.
_LISTENER_LOG_TS: Dict[str, float] = {}
_LISTENER_LOG_INTERVAL = 60.0


def notify_listeners(listeners: Iterable[Any], label: str, *args) -> None:
    """Invoke listeners in order, logging failures at most once per minute."""
    for listener in listeners:
        try:
            listener(*args)
        except Exception as exc:
            now = time.monotonic()
            if now - _LISTENER_LOG_TS.get(label, 0.0) >= _LISTENER_LOG_INTERVAL:
                _LISTENER_LOG_TS[label] = now
                logger.exception("Feed listener %s failed: %s", label, exc)
