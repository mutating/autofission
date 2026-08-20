"""Pytest fixtures for real Autofission/Fission end-to-end tests."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from .support import (
    PYTHON_ENV_IMAGE,
    E2ECluster,
    bootstrap_cluster,
    collect_diagnostics,
    require_tools,
    start_port_forward,
    stop_process,
)

E2E_ENABLED_ENV = 'AUTOFISSION_E2E'
KEEP_CLUSTER_ENV = 'AUTOFISSION_E2E_KEEP_CLUSTER'
ARTIFACTS_ENV = 'AUTOFISSION_E2E_ARTIFACTS'
ENVIRONMENT_NAME = 'autofission-e2e-python'

_FUNCTION_SOURCE = """\
import time


def main():
    deadline = time.monotonic() + 1.5
    value = 0
    while time.monotonic() < deadline:
        value = (value + 1) % 1000003
    return f"autofission-e2e:{value}\\n"
"""


@dataclass(frozen=True)
class FissionFunction:
    """A real Fission Function created for one test."""

    name: str
    path: str | None


class FunctionFactory:
    """Create isolated managed Functions and clean them after a test."""

    def __init__(self, cluster: E2ECluster, source: Path) -> None:
        self._cluster = cluster
        self._source = source
        self._created: list[FissionFunction] = []

    def __call__(
        self,
        *,
        cpu_millicores: int,
        memory_mebibytes: int = 128,
        minimum: int = 0,
        maximum: int = 1_000,
        target_cpu: int = 50,
        route: bool = False,
    ) -> FissionFunction:
        name = f'autofission-e2e-{uuid.uuid4().hex[:10]}'
        path = f'/{name}' if route else None
        self._cluster.fission(
            'function',
            'create',
            '--name',
            name,
            '--env',
            ENVIRONMENT_NAME,
            '--code',
            str(self._source),
            '--executortype',
            'newdeploy',
            '--minscale',
            str(minimum),
            '--maxscale',
            str(maximum),
            '--targetcpu',
            str(target_cpu),
            '--mincpu',
            str(cpu_millicores),
            '--maxcpu',
            str(cpu_millicores),
            '--minmemory',
            str(memory_mebibytes),
            '--maxmemory',
            str(memory_mebibytes),
            '--fntimeout',
            '30',
            '--labels',
            'autoscaling.fission.io/cluster-capacity=true',
        )
        function = FissionFunction(name, path)
        self._created.append(function)
        if path is not None:
            self._cluster.fission(
                'route',
                'create',
                '--name',
                f'{name}-route',
                '--method',
                'GET',
                '--url',
                path,
                '--function',
                name,
            )
        return function

    def cleanup(self) -> None:
        """Remove Functions and HTTPTriggers created through this factory."""
        for function in reversed(self._created):
            if function.path is not None:
                self._cluster.fission(
                    'route',
                    'delete',
                    '--name',
                    f'{function.name}-route',
                    check=False,
                )
            self._cluster.fission(
                'function',
                'delete',
                '--name',
                function.name,
                check=False,
            )


@pytest.fixture(scope='session')
def e2e_cluster(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[E2ECluster]:
    """Provision a disposable four-node Kind cluster for this pytest session."""
    if os.environ.get(E2E_ENABLED_ENV) != '1':
        pytest.skip(f'set {E2E_ENABLED_ENV}=1 to run tests against a disposable Kind cluster')

    require_tools(('docker', 'kind', 'kubectl', 'helm', 'fission'))
    workspace = tmp_path_factory.mktemp('autofission-e2e')
    artifacts_value = os.environ.get(ARTIFACTS_ENV)
    artifacts = (
        Path(artifacts_value).expanduser().resolve() if artifacts_value else workspace / 'artifacts'
    )
    cluster = E2ECluster(
        name=f'autofission-{uuid.uuid4().hex[:8]}',
        kubeconfig=workspace / 'kubeconfig',
        workspace=workspace,
        artifacts=artifacts,
    )

    try:
        bootstrap_cluster(cluster)
    except BaseException:
        collect_diagnostics(cluster)
        _delete_cluster(cluster)
        raise

    try:
        yield cluster
    finally:
        if request.session.testsfailed:
            collect_diagnostics(cluster)
        _delete_cluster(cluster)


@pytest.fixture(scope='session')
def fission_environment(e2e_cluster: E2ECluster) -> str:
    """Create a newdeploy Python Environment with the elastic PriorityClass."""
    e2e_cluster.fission(
        'environment',
        'create',
        '--name',
        ENVIRONMENT_NAME,
        '--image',
        PYTHON_ENV_IMAGE,
        '--poolsize',
        '0',
        '--version',
        '3',
    )
    e2e_cluster.kubectl(
        '-n',
        'default',
        'patch',
        'environments.fission.io',
        ENVIRONMENT_NAME,
        '--type=merge',
        '--patch',
        '{"spec":{"runtime":{"podspec":{"priorityClassName":"autofission-runtime"}}}}',
    )
    return ENVIRONMENT_NAME


@pytest.fixture
def function_factory(
    e2e_cluster: E2ECluster,
    fission_environment: str,
    tmp_path: Path,
) -> Iterator[FunctionFactory]:
    """Yield a per-test factory backed by the shared real Python Environment."""
    del fission_environment
    source = tmp_path / 'function.py'
    source.write_text(_FUNCTION_SOURCE, encoding='utf-8')
    factory = FunctionFactory(e2e_cluster, source)
    try:
        yield factory
    finally:
        factory.cleanup()


@pytest.fixture(scope='session')
def router_url(e2e_cluster: E2ECluster) -> Iterator[str]:
    """Expose the in-cluster Fission router only on the loopback hostname."""
    process, port = start_port_forward(e2e_cluster, 'fission', 'service/router', 80)
    try:
        yield f'http://localhost:{port}'
    finally:
        stop_process(process)


def _delete_cluster(cluster: E2ECluster) -> None:
    if os.environ.get(KEEP_CLUSTER_ENV) == '1':
        return
    if shutil.which('kind') is not None:
        cluster.run(
            ('kind', 'delete', 'cluster', '--name', cluster.name),
            timeout=300,
            check=False,
        )
