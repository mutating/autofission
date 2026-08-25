from __future__ import annotations

import signal
from pathlib import Path

import pytest

from autofission import __version__
from autofission.cli import _create_gateway, build_parser, main
from autofission.errors import ConfigurationError
from autofission.health import HealthFiles
from tests.helpers import environment, function, node


class FakeGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False
        self.patches: list[object] = []

    def list_nodes(self) -> list[object]:
        return [node()]

    def list_pods(self) -> list[object]:
        return []

    def list_functions(self, label_selector: str) -> list[object]:
        del label_selector
        if self.fail:
            raise RuntimeError('API unavailable')
        return [function()]

    def list_environments(self) -> list[object]:
        return [environment()]

    def patch_function(self, namespace: str, name: str, body: object) -> object:
        self.patches.append((namespace, name, body))
        return {}

    def close(self) -> None:
        self.closed = True


def test_create_gateway_delegates_to_official_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway()
    captured: dict[str, object] = {}

    def create(**kwargs: object) -> FakeGateway:
        captured.update(kwargs)
        return gateway

    monkeypatch.setattr('autofission.kubernetes.KubernetesClient.create', create)

    assert (
        _create_gateway(
            kubeconfig=Path('config'),
            context='test',
            in_cluster=False,
            request_timeout=7.5,
            retry_attempts=4,
        )
        is gateway
    )
    assert captured == {
        'kubeconfig': Path('config'),
        'context': 'test',
        'in_cluster': False,
        'request_timeout': 7.5,
        'retry_attempts': 4,
    }


def test_help_and_version_do_not_construct_kubernetes_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        'autofission.cli._create_gateway',
        lambda **_kwargs: pytest.fail('must not create client'),
    )

    with pytest.raises(SystemExit, match='0'):
        main(['--help'])
    assert 'capacity' in capsys.readouterr().out

    with pytest.raises(SystemExit, match='0'):
        main(['--version'])
    assert __version__ in capsys.readouterr().out


def test_probe_does_not_construct_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        'autofission.cli._create_gateway',
        lambda **_kwargs: pytest.fail('must not create client'),
    )
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr('autofission.health.time.time', lambda: 100)
    HealthFiles(state).mark_ready()

    assert (
        main(
            [
                '--probe',
                'readiness',
                '--state-directory',
                str(state),
                '--max-age-seconds',
                '10',
            ],
        )
        == 0
    )
    assert main(['--probe', 'liveness', '--state-directory', str(state)]) == 1


def test_once_runs_real_controller_and_closes_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway()
    monkeypatch.setattr(
        'autofission.cli._create_gateway',
        lambda **_kwargs: gateway,
    )
    handlers: dict[signal.Signals, object] = {}
    monkeypatch.setattr(
        'autofission.cli.signal.signal',
        lambda kind, callback: handlers.setdefault(kind, callback),
    )

    result = main(['--once', '--state-directory', str(tmp_path)])

    assert result == 0
    assert gateway.closed
    assert len(gateway.patches) == 1
    assert (tmp_path / 'ready').exists()
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}


def test_once_runtime_failure_returns_one_and_still_closes_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway(fail=True)
    monkeypatch.setattr(
        'autofission.cli._create_gateway',
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr('autofission.cli.signal.signal', lambda *_args: None)

    assert main(['--once', '--state-directory', str(tmp_path)]) == 1
    assert gateway.closed
    assert not (tmp_path / 'ready').exists()


def test_cli_arguments_override_environment_and_empty_environment_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('AUTOFISSION_INTERVAL_SECONDS', '99')
    monkeypatch.setenv('AUTOFISSION_FETCHER_CPU_REQUEST', '')

    arguments = build_parser().parse_args(
        ['--interval-seconds', '7', '--fetcher-memory-request', '32Mi'],
    )

    assert arguments.interval_seconds == 7
    assert arguments.fetcher_cpu_request == '10m'
    assert arguments.fetcher_memory_request == '32Mi'


def test_runtime_priority_class_can_be_set_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('AUTOFISSION_RUNTIME_PRIORITY_CLASS', 'elastic-runtime')

    assert build_parser().parse_args([]).runtime_priority_class == 'elastic-runtime'


def test_boolean_environment_and_cli_negation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('AUTOFISSION_INCLUDE_TAINTED_NODES', 'TRUE')
    assert build_parser().parse_args([]).include_tainted_nodes is True
    assert build_parser().parse_args(['--no-include-tainted-nodes']).include_tainted_nodes is False


@pytest.mark.parametrize(
    ('variable', 'value'),
    [
        ('AUTOFISSION_INTERVAL_SECONDS', 'zero'),
        ('AUTOFISSION_RETRY_ATTEMPTS', '-1'),
        ('AUTOFISSION_INCLUDE_TAINTED_NODES', 'yes'),
    ],
)
def test_invalid_environment_returns_configuration_exit_code(
    variable: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(variable, value)
    assert main(['--probe', 'readiness']) == 2
    assert 'autofission:' in capsys.readouterr().err


@pytest.mark.parametrize(
    'arguments',
    [
        ['--interval-seconds', '0'],
        ['--interval-seconds', '-1'],
        ['--interval-seconds', 'nan'],
        ['--interval-seconds', 'inf'],
        ['--retry-attempts', '-1'],
        ['--retry-attempts', '1.5'],
        ['--max-age-seconds', '0'],
    ],
)
def test_invalid_cli_numbers_exit_two(arguments: list[str]) -> None:
    with pytest.raises(SystemExit, match='2'):
        main(arguments)


@pytest.mark.parametrize(
    'arguments',
    [
        ['--fetcher-cpu-request', 'bad'],
        ['--fetcher-memory-request', '-1'],
        ['--managed-label', ''],
        ['--managed-value', ''],
    ],
)
def test_invalid_controller_configuration_fails_before_client(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        'autofission.cli._create_gateway',
        lambda **_kwargs: pytest.fail('must fail before client creation'),
    )
    assert main(arguments) == 2
    assert capsys.readouterr().err.startswith('autofission:')


def test_client_configuration_error_returns_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(**_kwargs: object) -> object:
        raise ConfigurationError('no credentials')

    monkeypatch.setattr('autofission.cli._create_gateway', fail)

    assert main([]) == 2
    assert 'no credentials' in capsys.readouterr().err


def test_signal_handler_requests_cooperative_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway()
    callbacks: list[object] = []
    monkeypatch.setattr(
        'autofission.cli._create_gateway',
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        'autofission.cli.signal.signal',
        lambda _kind, callback: callbacks.append(callback),
    )

    class StoppingRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.stop_event = SimpleStop()

        def run(self, *, once: bool = False) -> int:
            del once
            callbacks[0](signal.SIGTERM, None)  # type: ignore[operator]
            return 0 if self.stop_event.stopped else 1

    class SimpleStop:
        stopped = False

        def set(self) -> None:
            self.stopped = True

    monkeypatch.setattr('autofission.cli.Runner', StoppingRunner)

    assert main(['--state-directory', str(tmp_path)]) == 0
