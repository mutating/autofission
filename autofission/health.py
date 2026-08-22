"""Filesystem health markers used by Kubernetes exec probes."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from math import isfinite
from pathlib import Path


@dataclass(frozen=True)
class HealthFiles:
    """Manage process-local readiness and liveness timestamps."""

    directory: Path

    @property
    def readiness_path(self) -> Path:
        return self.directory / 'ready'

    @property
    def liveness_path(self) -> Path:
        return self.directory / 'live'

    def reset(self) -> None:
        """Remove markers that may have survived a container restart."""
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in (self.readiness_path, self.liveness_path):
            path.unlink(missing_ok=True)

    def mark_ready(self) -> None:
        self._mark(self.readiness_path)

    def mark_live(self) -> None:
        self._mark(self.liveness_path)

    @staticmethod
    def _mark(path: Path) -> None:
        path.touch()
        timestamp = time.time()
        os.utime(path, (timestamp, timestamp))

    def is_fresh(self, kind: str, max_age_seconds: float, now: float | None = None) -> bool:
        """Return true only for an existing, non-future marker within max age."""
        if kind not in {'readiness', 'liveness'}:
            raise ValueError(f'unknown probe kind: {kind!r}')
        if not isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError('probe max age must be finite and positive')
        path = self.readiness_path if kind == 'readiness' else self.liveness_path
        try:
            modified = path.stat().st_mtime
        except OSError:
            return False
        age = (time.time() if now is None else now) - modified
        return 0 <= age <= max_age_seconds
