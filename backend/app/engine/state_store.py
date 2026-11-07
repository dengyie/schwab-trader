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

DEFAULT_STATE_SUBDIR = ".schwab-trader"


def resolve_state_dir(configured: str = "") -> Optional[str]:
    """Pick a directory the running process can actually write.

    Preference: an explicit ``STATE_DIR``, then ``<cwd>/data`` (the deployed
    backend tree, kept by the deploy), then ``~/.schwab-trader`` for hosts where
    the code directory belongs to the deploy user rather than the service user.
    Returns ``None`` when nothing is writable, which disables persistence rather
    than failing every order.
    """
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    else:
        candidates.append(Path.cwd() / "data")
        candidates.append(Path.home() / DEFAULT_STATE_SUBDIR)

    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return str(directory)
        except OSError as exc:
            logger.warning("State dir %s unusable: %s", directory, exc)
    logger.warning("No writable state dir; the virtual book will not persist")
    return None


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
                self.quarantine("corrupt", detail=str(exc))
                return None
            if not isinstance(snapshot, dict):
                logger.warning("Virtual book at %s is not an object; ignoring", self.path)
                return None
            return snapshot

    def quarantine(self, reason: str = "rejected", detail: str = "") -> None:
        """Move the snapshot aside so it is never rebuilt from again.

        A structurally invalid file that stays in place would be re-read and
        re-rejected on every boot, so the same half-broken book would come back
        each time. Renaming keeps the bytes for inspection and lets the engine
        write a clean snapshot in its place.
        """
        with self._lock:
            if not self.path.exists():
                return
            target = self.path.with_suffix(
                f".{reason}-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            try:
                os.replace(self.path, target)
                logger.warning(
                    "Virtual book at %s quarantined to %s%s",
                    self.path,
                    target,
                    f": {detail}" if detail else "",
                )
            except OSError as exc:
                logger.warning("Virtual book at %s could not be quarantined: %s", self.path, exc)

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
