"""Disposable Kind-cluster orchestration used by the e2e pytest fixtures."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / 'deploy' / 'helm' / 'autofission'
FISSION_VERSION = '1.27.0'
METRICS_SERVER_CHART_VERSION = '3.13.1'
KIND_NODE_IMAGE = (
    'kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a'
)
PYTHON_ENV_IMAGE = (
    'ghcr.io/fission/python-env:1.35.0@sha256:'
    'f6c8c470c084ce7ecadcaaca76717a7ecaf24b64f5d85a94b1bafa45529b814c'
)
WORKER_LABEL = 'autofission.io/e2e-worker=true'
PROTECTED_LABEL = 'app.kubernetes.io/name=autofission-e2e-protected'

_KIND_CONFIG = """\
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
  - role: worker
"""

T = TypeVar('T')


def require_tools(names: Sequence[str]) -> None:
    """Fail early with one actionable message when local e2e prerequisites are absent."""
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f'missing e2e executables: {", ".join(missing)}')


@dataclass(frozen=True)
class E2ECluster:
    """Command adapter bound to one isolated kubeconfig and Kind cluster."""

    name: str
    kubeconfig: Path
    workspace: Path
    artifacts: Path

    @property
    def environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment['KUBECONFIG'] = str(self.kubeconfig)
        return environment

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: float = 120,
        check: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=self.environment,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            rendered = ' '.join(command)
            raise AssertionError(
                f'command failed ({result.returncode}): {rendered}\n'
                f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}',
            )
        return result

    def kubectl(
        self,
        *arguments: str,
        input_text: str | None = None,
        timeout: float = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            ('kubectl', '--kubeconfig', str(self.kubeconfig), *arguments),
            input_text=input_text,
            timeout=timeout,
            check=check,
        )

    def kubectl_json(self, *arguments: str, timeout: float = 120) -> Mapping[str, object]:
        result = self.kubectl(*arguments, '-o', 'json', timeout=timeout)
        document = json.loads(result.stdout)
        if not isinstance(document, Mapping):
            raise TypeError(f'kubectl returned non-object JSON for {arguments!r}')
        return cast(Mapping[str, object], document)

    def apply(self, document: Mapping[str, object]) -> None:
        manifest = yaml.safe_dump(dict(document), sort_keys=False)
        self.kubectl('apply', '-f', '-', input_text=manifest)

    def helm(self, *arguments: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        return self.run(
            ('helm', '--kubeconfig', str(self.kubeconfig), *arguments),
            timeout=timeout,
        )

    def fission(
        self,
        *arguments: str,
        timeout: float = 300,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(('fission', *arguments), timeout=timeout, check=check)

    def wait_for(
        self,
        description: str,
        predicate: Callable[[], T | None],
        *,
        timeout: float = 180,
        interval: float = 2,
    ) -> T:
        deadline = time.monotonic() + timeout
        last_error: AssertionError | TypeError | None = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value is not None and value is not False:
                    return value
            except (AssertionError, TypeError) as error:
                last_error = error
            time.sleep(interval)
        detail = '' if last_error is None else f' Last error: {last_error}'
        raise AssertionError(f'timed out waiting for {description}.{detail}')


def create_kind_cluster(cluster: E2ECluster) -> None:
    """Create one control plane and exactly three schedulable worker nodes."""
    config_path = cluster.workspace / 'kind.yaml'
    config_path.write_text(_KIND_CONFIG, encoding='utf-8')
    cluster.run(
        (
            'kind',
            'create',
            'cluster',
            '--name',
            cluster.name,
            '--image',
            KIND_NODE_IMAGE,
            '--config',
            str(config_path),
            '--kubeconfig',
            str(cluster.kubeconfig),
            '--wait',
            '5m',
        ),
        timeout=600,
    )
    cluster.kubectl('wait', 'node', '--all', '--for=condition=Ready', '--timeout=5m')

    nodes = _items(cluster.kubectl_json('get', 'nodes'), 'nodes')
    control_planes: list[str] = []
    workers: list[str] = []
    for node in nodes:
        metadata = _mapping(node.get('metadata'), 'node.metadata')
        labels = _mapping(metadata.get('labels', {}), 'node.metadata.labels')
        name = _string(metadata.get('name'), 'node.metadata.name')
        if 'node-role.kubernetes.io/control-plane' in labels:
            control_planes.append(name)
        else:
            workers.append(name)
    if len(control_planes) != 1 or len(workers) != 3:
        raise AssertionError(
            f'expected one control plane and three workers, got {control_planes=} {workers=}',
        )
    cluster.kubectl('cordon', control_planes[0])
    cluster.kubectl('label', 'nodes', *workers, WORKER_LABEL, '--overwrite')


def build_and_load_controller(cluster: E2ECluster) -> tuple[str, str]:
    """Build the current checkout into an OCI image and load it on every Kind node."""
    context = cluster.workspace / 'image'
    distribution = context / 'dist'
    distribution.mkdir(parents=True)
    cluster.run(
        (sys.executable, '-m', 'build', '--wheel', '--outdir', str(distribution)),
        cwd=ROOT,
        timeout=300,
    )
    shutil.copy2(ROOT / 'Dockerfile', context / 'Dockerfile')
    repository = 'autofission'
    tag = f'e2e-{cluster.name}'
    image = f'{repository}:{tag}'
    cluster.run(
        ('docker', 'build', '--tag', image, '--build-arg', 'VERSION=e2e', '.'),
        cwd=context,
        timeout=600,
    )
    cluster.run(
        ('kind', 'load', 'docker-image', '--name', cluster.name, image),
        timeout=600,
    )
    return repository, tag


def install_fission(cluster: E2ECluster) -> None:
    """Install the upstream CRDs and Fission chart pinned to one release."""
    cluster.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'fission'}})
    cluster.kubectl(
        'apply',
        '--server-side',
        '-k',
        f'github.com/fission/fission/crds/v1?ref=v{FISSION_VERSION}',
        timeout=300,
    )
    cluster.helm(
        'repo',
        'add',
        'fission-charts',
        'https://fission.github.io/fission-charts/',
        '--force-update',
    )
    cluster.helm(
        'upgrade',
        '--install',
        'fission',
        'fission-charts/fission-all',
        '--namespace',
        'fission',
        '--version',
        FISSION_VERSION,
        '--set',
        'serviceType=ClusterIP',
        '--set',
        'routerServiceType=ClusterIP',
        '--wait',
        '--timeout',
        '10m',
        timeout=900,
    )
    cluster.fission('check', timeout=300)


def install_metrics_server(cluster: E2ECluster) -> None:
    """Install Metrics Server with the Kind-specific kubelet TLS option."""
    cluster.helm(
        'repo',
        'add',
        'metrics-server',
        'https://kubernetes-sigs.github.io/metrics-server/',
        '--force-update',
    )
    cluster.helm(
        'upgrade',
        '--install',
        'metrics-server',
        'metrics-server/metrics-server',
        '--namespace',
        'kube-system',
        '--version',
        METRICS_SERVER_CHART_VERSION,
        '--set',
        'args[0]=--kubelet-insecure-tls',
        '--wait',
        '--timeout',
        '5m',
        timeout=600,
    )

    def metrics_are_available() -> bool | None:
        result = cluster.kubectl('top', 'nodes', check=False)
        return True if result.returncode == 0 else None

    cluster.wait_for('Metrics API samples', metrics_are_available, timeout=180, interval=5)


def install_autofission(cluster: E2ECluster, repository: str, tag: str) -> None:
    """Install the chart under test with a fast reconciliation interval."""
    cluster.helm(
        'upgrade',
        '--install',
        'autofission',
        str(CHART),
        '--namespace',
        'fission',
        '--set-string',
        f'image.repository={repository}',
        '--set-string',
        f'image.tag={tag}',
        '--set',
        'image.pullPolicy=Never',
        '--set',
        'controller.intervalSeconds=2',
        '--set',
        'controller.requestTimeoutSeconds=5',
        '--set',
        'controller.retryAttempts=1',
        '--set',
        'readinessProbe.initialDelaySeconds=1',
        '--set',
        'readinessProbe.periodSeconds=2',
        '--set',
        'readinessProbe.maxAgeSeconds=20',
        '--set',
        'livenessProbe.initialDelaySeconds=10',
        '--set',
        'livenessProbe.periodSeconds=5',
        '--set',
        'livenessProbe.maxAgeSeconds=60',
        '--wait',
        '--timeout',
        '5m',
        timeout=600,
    )
    cluster.kubectl(
        '-n',
        'fission',
        'rollout',
        'status',
        'deployment/autofission',
        '--timeout=5m',
    )


def create_protected_workload(cluster: E2ECluster) -> None:
    """Place one ordinary workload Pod on each worker before elastic work starts."""
    cluster.apply(
        {
            'apiVersion': 'apps/v1',
            'kind': 'DaemonSet',
            'metadata': {'name': 'autofission-e2e-protected', 'namespace': 'default'},
            'spec': {
                'selector': {
                    'matchLabels': {'app.kubernetes.io/name': 'autofission-e2e-protected'},
                },
                'template': {
                    'metadata': {
                        'labels': {'app.kubernetes.io/name': 'autofission-e2e-protected'},
                    },
                    'spec': {
                        'nodeSelector': {'autofission.io/e2e-worker': 'true'},
                        'containers': [
                            {
                                'name': 'protected',
                                'image': 'registry.k8s.io/pause:3.10',
                                'resources': {
                                    'requests': {'cpu': '250m', 'memory': '32Mi'},
                                    'limits': {'cpu': '250m', 'memory': '32Mi'},
                                },
                            },
                        ],
                    },
                },
            },
        },
    )
    cluster.kubectl(
        '-n',
        'default',
        'rollout',
        'status',
        'daemonset/autofission-e2e-protected',
        '--timeout=5m',
    )
    pods = _items(
        cluster.kubectl_json('get', 'pods', '-n', 'default', '-l', PROTECTED_LABEL),
        'protected pods',
    )
    if len(pods) != 3:
        raise AssertionError(f'expected three protected Pods, got {len(pods)}')


def bootstrap_cluster(cluster: E2ECluster) -> None:
    """Create the complete real system exercised by the e2e tests."""
    create_kind_cluster(cluster)
    repository, tag = build_and_load_controller(cluster)
    install_fission(cluster)
    install_metrics_server(cluster)
    install_autofission(cluster, repository, tag)
    create_protected_workload(cluster)


def collect_diagnostics(cluster: E2ECluster) -> None:
    """Persist non-secret cluster state when setup or a test fails."""
    cluster.artifacts.mkdir(parents=True, exist_ok=True)
    commands = {
        'nodes.txt': ('get', 'nodes', '-o', 'wide'),
        'pods.txt': ('get', 'pods', '--all-namespaces', '-o', 'wide'),
        'workloads.yaml': ('get', 'deployments,daemonsets,hpa', '--all-namespaces', '-o', 'yaml'),
        'fission-resources.yaml': (
            'get',
            'functions.fission.io,environments.fission.io',
            '--all-namespaces',
            '-o',
            'yaml',
        ),
        'events.txt': (
            'get',
            'events',
            '--all-namespaces',
            '--sort-by=.metadata.creationTimestamp',
        ),
        'autofission.log': (
            'logs',
            '-n',
            'fission',
            'deployment/autofission',
            '--all-containers=true',
        ),
    }
    for filename, arguments in commands.items():
        result = cluster.kubectl(*arguments, check=False, timeout=180)
        (cluster.artifacts / filename).write_text(
            f'{result.stdout}\n{result.stderr}',
            encoding='utf-8',
        )
    cluster.run(
        ('kind', 'export', 'logs', str(cluster.artifacts / 'kind'), '--name', cluster.name),
        timeout=300,
        check=False,
    )


def start_port_forward(
    cluster: E2ECluster,
    namespace: str,
    resource: str,
    remote_port: int,
) -> tuple[subprocess.Popen[str], int]:
    """Start kubectl port-forward on an unused localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('localhost', 0))
        port = cast(int, listener.getsockname()[1])
    process = subprocess.Popen(
        [
            'kubectl',
            '--kubeconfig',
            str(cluster.kubeconfig),
            '-n',
            namespace,
            'port-forward',
            resource,
            f'{port}:{remote_port}',
        ],
        env=cluster.environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def forwarding() -> bool | None:
        if process.poll() is not None:
            output = '' if process.stdout is None else process.stdout.read()
            raise AssertionError(f'port-forward exited early: {output}')
        try:
            with socket.create_connection(('localhost', port), timeout=0.5):
                return True
        except OSError:
            return None

    cluster.wait_for('router port-forward', forwarding, timeout=30, interval=0.25)
    return process, port


def stop_process(process: subprocess.Popen[str]) -> None:
    """Terminate a helper subprocess without leaking it across test sessions."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f'{context} must be an object')
    return cast(Mapping[str, object], value)


def _items(document: Mapping[str, object], context: str) -> list[Mapping[str, object]]:
    value = document.get('items')
    if not isinstance(value, list):
        raise TypeError(f'{context}.items must be a list')
    return [_mapping(item, f'{context} item') for item in value]


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssertionError(f'{context} must be a non-empty string')
    return value
