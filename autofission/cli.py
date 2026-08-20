"""Command-line interface for Autofission."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from autofission import __version__
from autofission.controller import (
    MANAGED_LABEL,
    MANAGED_VALUE,
    Controller,
    ControllerConfig,
    KubernetesGateway,
)
from autofission.errors import AutofissionError
from autofission.health import HealthFiles
from autofission.models import Resources
from autofission.quantities import parse_cpu_millicores, parse_memory_bytes
from autofission.runner import Runner

_ENV_PREFIX = 'AUTOFISSION_'


class _ClosableGateway(KubernetesGateway, Protocol):
    def close(self) -> None: ...


def _create_gateway(
    *,
    kubeconfig: Path | None,
    context: str | None,
    in_cluster: bool | None,
    request_timeout: float,
    retry_attempts: int,
) -> _ClosableGateway:
    # Keep help, version, and health probes lightweight on constrained nodes.
    from autofission.kubernetes import KubernetesClient  # noqa: PLC0415

    return KubernetesClient.create(
        kubeconfig=kubeconfig,
        context=context,
        in_cluster=in_cluster,
        request_timeout=request_timeout,
        retry_attempts=retry_attempts,
    )


def _env(name: str, default: str) -> str:
    value = os.environ.get(f'{_ENV_PREFIX}{name}')
    if value is None or value == '':
        return default
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name, str(default).lower()).lower()
    if value not in {'true', 'false'}:
        raise argparse.ArgumentTypeError('must be true or false')
    return value == 'true'


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('must be a number') from error
    if not parsed > 0 or parsed == float('inf'):
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('must be an integer') from error
    if parsed < 0:
        raise argparse.ArgumentTypeError('must not be negative')
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser without reading Kubernetes credentials."""
    parser = argparse.ArgumentParser(
        prog='autofission',
        description='Set Fission MaxScale from safely available Kubernetes capacity.',
    )
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--once', action='store_true', help='reconcile once and exit')
    parser.add_argument(
        '--interval-seconds',
        type=_positive_float,
        default=_positive_float(_env('INTERVAL_SECONDS', '15')),
    )
    parser.add_argument(
        '--request-timeout-seconds',
        type=_positive_float,
        default=_positive_float(_env('REQUEST_TIMEOUT_SECONDS', '10')),
    )
    parser.add_argument(
        '--retry-attempts',
        type=_non_negative_int,
        default=_non_negative_int(_env('RETRY_ATTEMPTS', '3')),
    )
    parser.add_argument(
        '--fetcher-cpu-request',
        default=_env('FETCHER_CPU_REQUEST', '10m'),
    )
    parser.add_argument(
        '--fetcher-memory-request',
        default=_env('FETCHER_MEMORY_REQUEST', '16Mi'),
    )
    parser.add_argument(
        '--managed-label',
        default=_env('MANAGED_LABEL', MANAGED_LABEL),
    )
    parser.add_argument(
        '--managed-value',
        default=_env('MANAGED_VALUE', MANAGED_VALUE),
    )
    parser.add_argument(
        '--include-tainted-nodes',
        action=argparse.BooleanOptionalAction,
        default=_env_bool('INCLUDE_TAINTED_NODES'),
    )
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--context')
    parser.add_argument('--in-cluster', action='store_true', default=None)
    parser.add_argument(
        '--state-directory',
        type=Path,
        default=Path(_env('STATE_DIRECTORY', '/tmp/autofission')),
    )
    parser.add_argument('--probe', choices=('readiness', 'liveness'))
    parser.add_argument(
        '--max-age-seconds',
        type=_positive_float,
        default=_positive_float(_env('MAX_AGE_SECONDS', '60')),
    )
    parser.add_argument(
        '--log-level',
        choices=('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'),
        default=_env('LOG_LEVEL', 'INFO').upper(),
    )
    return parser


def _configure_logging(level: str) -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    return logging.getLogger('autofission')


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    try:
        arguments = build_parser().parse_args(argv)
        health = HealthFiles(arguments.state_directory)
        if arguments.probe:
            return 0 if health.is_fresh(arguments.probe, arguments.max_age_seconds) else 1

        logger = _configure_logging(arguments.log_level)
        fetcher = Resources(
            parse_cpu_millicores(arguments.fetcher_cpu_request),
            parse_memory_bytes(arguments.fetcher_memory_request),
        )
        controller_config = ControllerConfig(
            fetcher_request=fetcher,
            managed_label=arguments.managed_label,
            managed_value=arguments.managed_value,
            include_tainted_nodes=arguments.include_tainted_nodes,
        )
        gateway = _create_gateway(
            kubeconfig=arguments.kubeconfig,
            context=arguments.context,
            in_cluster=arguments.in_cluster,
            request_timeout=arguments.request_timeout_seconds,
            retry_attempts=arguments.retry_attempts,
        )
        try:
            runner = Runner(
                Controller(gateway, controller_config, logger),
                health,
                arguments.interval_seconds,
                logger=logger,
            )

            def stop(_signum: int, _frame: object) -> None:
                logger.info('shutdown requested')
                runner.stop_event.set()

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            return runner.run(once=arguments.once)
        finally:
            gateway.close()
    except (argparse.ArgumentTypeError, AutofissionError, ValueError, OSError) as error:
        sys.stderr.write(f'autofission: {error}\n')
        return 2
