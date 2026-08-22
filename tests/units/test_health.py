import os
from pathlib import Path

import pytest

from autofission.health import HealthFiles


def test_reset_removes_stale_files_and_marks_both_states(tmp_path: Path) -> None:
    health = HealthFiles(tmp_path / 'state')
    health.directory.mkdir()
    health.readiness_path.write_text('stale')
    health.liveness_path.write_text('stale')

    health.reset()

    assert not health.readiness_path.exists()
    assert not health.liveness_path.exists()
    health.mark_ready()
    health.mark_live()
    assert health.readiness_path.is_file()
    assert health.liveness_path.is_file()


def test_reset_creates_missing_parent_directories(tmp_path: Path) -> None:
    health = HealthFiles(tmp_path / 'nested' / 'state')
    health.reset()
    assert health.directory.is_dir()


@pytest.mark.parametrize('kind', ['readiness', 'liveness'])
def test_mark_uses_the_health_clock(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    health = HealthFiles(tmp_path)
    monkeypatch.setattr('autofission.health.time.time', lambda: 100)

    if kind == 'readiness':
        health.mark_ready()
    else:
        health.mark_live()

    assert health.is_fresh(kind, 10, now=100)


@pytest.mark.parametrize('kind', ['readiness', 'liveness'])
def test_is_fresh_accepts_boundary_age(kind: str, tmp_path: Path) -> None:
    health = HealthFiles(tmp_path)
    path = health.readiness_path if kind == 'readiness' else health.liveness_path
    path.touch()
    os.utime(path, (100, 100))

    assert health.is_fresh(kind, 10, now=100)
    assert health.is_fresh(kind, 10, now=110)
    assert not health.is_fresh(kind, 10, now=111)
    assert not health.is_fresh(kind, 10, now=99)


def test_is_fresh_uses_current_clock_when_not_injected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    health = HealthFiles(tmp_path)
    health.readiness_path.touch()
    os.utime(health.readiness_path, (100, 100))
    monkeypatch.setattr('autofission.health.time.time', lambda: 105)
    assert health.is_fresh('readiness', 10)


def test_is_fresh_returns_false_when_marker_cannot_be_statted(tmp_path: Path) -> None:
    assert not HealthFiles(tmp_path).is_fresh('readiness', 1)


@pytest.mark.parametrize('kind', ['', 'ready', 'unknown'])
def test_is_fresh_rejects_unknown_probe_kind(kind: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='unknown probe'):
        HealthFiles(tmp_path).is_fresh(kind, 1)


@pytest.mark.parametrize('age', [0.0, -1.0, float('nan'), float('inf')])
def test_is_fresh_rejects_invalid_max_age(age: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='finite and positive'):
        HealthFiles(tmp_path).is_fresh('readiness', age)
