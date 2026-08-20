"""Long-running reconciliation loop."""

from __future__ import annotations

import logging
import threading
from math import isfinite
from typing import Protocol

from autofission.health import HealthFiles


class Reconciler(Protocol):
    def reconcile(self) -> object: ...


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...

    def set(self) -> None: ...


class Runner:
    """Execute reconciliation once or until a stop event is set."""

    def __init__(
        self,
        controller: Reconciler,
        health: HealthFiles,
        interval_seconds: float,
        stop_event: StopEvent | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError('reconcile interval must be finite and positive')
        self._controller = controller
        self._health = health
        self._interval = interval_seconds
        self._stop = stop_event or threading.Event()
        self._logger = logger or logging.getLogger('autofission')

    @property
    def stop_event(self) -> StopEvent:
        return self._stop

    def run(self, *, once: bool = False) -> int:
        """Return nonzero for a failed one-shot; daemons retry failures."""
        self._health.reset()
        while not self._stop.is_set():
            self._health.mark_live()
            try:
                self._controller.reconcile()
                self._health.mark_ready()
            except Exception:
                self._logger.exception('reconciliation failed')
                if once:
                    return 1
            finally:
                self._health.mark_live()

            if once:
                return 0
            self._stop.wait(self._interval)
        return 0
