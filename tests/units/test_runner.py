from __future__ import annotations

import logging
from pathlib import Path

import pytest

from autofission.health import HealthFiles
from autofission.runner import Runner


class FakeController:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def reconcile(self) -> object:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if outcome is not None:
            raise outcome
        return object()


class ImmediateEvent:
    def __init__(self, waits_before_stop: int) -> None:
        self.waits_before_stop = waits_before_stop
        self.waits: list[float] = []
        self.forced = False

    def is_set(self) -> bool:
        return self.forced or len(self.waits) >= self.waits_before_stop

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.is_set()

    def set(self) -> None:
        self.forced = True


def test_one_shot_success_reconciles_once_and_marks_health(tmp_path: Path) -> None:
    controller = FakeController([None])
    health = HealthFiles(tmp_path)
    runner = Runner(controller, health, 15)

    assert runner.run(once=True) == 0
    assert controller.calls == 1
    assert health.is_fresh('readiness', 10)
    assert health.is_fresh('liveness', 10)


def test_one_shot_failure_returns_one_without_readiness(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    controller = FakeController([RuntimeError('broken')])
    health = HealthFiles(tmp_path)

    with caplog.at_level(logging.ERROR):
        result = Runner(controller, health, 15).run(once=True)

    assert result == 1
    assert not health.readiness_path.exists()
    assert health.liveness_path.exists()
    assert 'reconciliation failed' in caplog.text


def test_daemon_recovers_after_transient_failure_and_waits_cooperatively(
    tmp_path: Path,
) -> None:
    controller = FakeController([RuntimeError('transient'), None])
    stop = ImmediateEvent(waits_before_stop=2)
    health = HealthFiles(tmp_path)
    runner = Runner(
        controller,
        health,
        7,
        stop_event=stop,
    )

    assert runner.run() == 0
    assert controller.calls == 2
    assert stop.waits == [7, 7]
    assert health.readiness_path.exists()


def test_preexisting_stop_event_performs_no_reconcile_but_resets_stale_state(
    tmp_path: Path,
) -> None:
    controller = FakeController([])
    stop = ImmediateEvent(waits_before_stop=0)
    health = HealthFiles(tmp_path)
    health.mark_ready()

    runner = Runner(controller, health, 1, stop_event=stop)

    assert runner.stop_event is stop
    assert runner.run() == 0
    assert controller.calls == 0
    assert not health.readiness_path.exists()


@pytest.mark.parametrize('interval', [0.0, -1.0, float('nan'), float('inf')])
def test_runner_rejects_invalid_interval(interval: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='finite and positive'):
        Runner(FakeController([]), HealthFiles(tmp_path), interval)
