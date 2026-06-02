"""
Simple file-based cache with TTL for IEC-1 data fetching.

Prevents re-hitting yfinance and Screener.in on repeated runs within the same day.
Cache is keyed by (source, ticker, date) and stored as pickle files.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FileCache:
    """
    Disk-backed cache with per-entry TTL.

    Parameters
    ----------
    cache_dir : str | Path
        Directory to store cache files. Created if it doesn't exist.
    default_ttl_hours : float
        Default time-to-live in hours. After expiry the entry is refetched.
        Set to 0 to disable caching.
    """

    def __init__(
        self,
        cache_dir: str | Path = "./inec1_cache",
        default_ttl_hours: float = 23.0,   # refresh once per day
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_ttl = timedelta(hours=default_ttl_hours)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, ttl_hours: Optional[float] = None) -> Optional[Any]:
        """
        Return cached value for `key` if it exists and is not expired.
        Returns None on cache miss or expiry.
        """
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                entry = pickle.load(f)
            ttl = timedelta(hours=ttl_hours) if ttl_hours is not None else self.default_ttl
            if datetime.utcnow() - entry["ts"] > ttl:
                path.unlink(missing_ok=True)
                return None
            return entry["value"]
        except Exception as e:
            logger.debug(f"Cache read error for {key}: {e}")
            return None

    def set(self, key: str, value: Any) -> None:
        """Store `value` under `key` with the current timestamp."""
        path = self._path(key)
        try:
            with open(path, "wb") as f:
                pickle.dump({"ts": datetime.utcnow(), "value": value}, f)
        except Exception as e:
            logger.debug(f"Cache write error for {key}: {e}")

    def invalidate(self, key: str) -> None:
        """Remove a specific cache entry."""
        self._path(key).unlink(missing_ok=True)

    def clear(self) -> int:
        """Delete all cache files. Returns count deleted."""
        count = 0
        for f in self.cache_dir.glob("*.pkl"):
            f.unlink()
            count += 1
        return count

    def clear_expired(self, ttl_hours: Optional[float] = None) -> int:
        """Delete only expired entries. Returns count deleted."""
        ttl = timedelta(hours=ttl_hours) if ttl_hours is not None else self.default_ttl
        count = 0
        for path in self.cache_dir.glob("*.pkl"):
            try:
                with open(path, "rb") as f:
                    entry = pickle.load(f)
                if datetime.utcnow() - entry["ts"] > ttl:
                    path.unlink()
                    count += 1
            except Exception:
                path.unlink(missing_ok=True)
                count += 1
        return count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _path(self, key: str) -> Path:
        """Hash the key to a safe filename."""
        h = hashlib.md5(key.encode()).hexdigest()
        return self.cache_dir / f"{h}.pkl"


def make_key(*parts: str) -> str:
    """Build a cache key from multiple parts."""
    return "|".join(str(p) for p in parts)
