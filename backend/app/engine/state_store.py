"""
Durable snapshot of the virtual trading book.

US orders rest on Alpaca, which is the system of record across restarts. The
HK / JP / CN book is matched in-process, so without a snapshot every deploy
silently liquidates it: positions, cash and working orders vanish while the
terminal still says they were bought. This store gives that book the same
survival guarantee.

Writes are atomic (temp file + rename) so a crash mid-write can never leave a
half-parsed book behind; a file that does fail to parse is quarantined rather
than silently ignored.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class JsonStateStore:
    """Single-file JSON snapshot with atomic replace and corruption quarantine."""

    def __init__(self, directory: str, filename: str = "virtual_book.json"):
        self.directory = Path(directory)
        self.path = self.directory / filename
        self._lock = threading.RLock()

    def load(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError as exc:
                logger.warning("Virtual book unreadable at %s: %s", self.path, exc)
                return None
            try:
                snapshot = json.loads(raw)
            except ValueError as exc:
                # Never drop the bytes: keep them for inspection and start clean.
                quarantine = self.path.with_suffix(
                    f".corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
                )
                try:
                    os.replace(self.path, quarantine)
                    logger.warning(
                        "Virtual book at %s was corrupt (%s); moved to %s",
                        self.path, exc, quarantine,
                    )
                except OSError:
                    logger.warning("Virtual book at %s was corrupt: %s", self.path, exc)
                return None
            if not isinstance(snapshot, dict):
                logger.warning("Virtual book at %s is not an object; ignoring", self.path)
                return None
            return snapshot

    def save(self, snapshot: Dict[str, Any]) -> bool:
        """Atomically replace the snapshot. Returns False if the write failed."""
        with self._lock:
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(snapshot, fh, separators=(",", ":"))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                logger.warning("Virtual book save failed at %s: %s", self.path, exc)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return False
