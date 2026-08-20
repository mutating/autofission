"""Real-cluster tests for expansion, contraction, and scheduler isolation."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import pytest

from autofission.controller import ANNOTATION_PREFIX
from autofission.quantities import parse_cpu_millicores
from tests.e2e.conftest import FissionFunction, FunctionFactory
from tests.e2e.support import PROTECTED_LABEL, WORKER_LABEL, E2ECluster

pytestmark = pytest.mark.e2e


@dataclass(frozen=True)
class _Workload:
    namespace: str
    name: str
    document: Mapping[str, object]


class _LoadGenerator:
    def __init__(self, url: str, concurrency: int) -> None:
        self._url = url
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(target=self._run, name=f'e2e-load-{index}', daemon=True)
            for index in range(concurrency)
        ]
        self.successes = 0

    def __enter__(self) -> _LoadGenerator:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *error: object) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=35)
        if any(thread.is_alive() for thread in self._threads):
            raise AssertionError('HTTP load threads did not stop')

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._request_succeeded():
                with self._lock:
                    self.successes += 1
            elif not self._stop.is_set():
                time.sleep(0.1)

    def _request_succeeded(self) -> bool:
        try:
            with urllib.request.urlopen(self._url, timeout=25) as response:
                body = response.read().decode('utf-8')
                status = response.status
            return isinstance(status, int) and status == 200 and body.startswith('autofission-e2e:')
        except (OSError, TimeoutError, urllib.error.HTTPError):
            return False


def test_real_fission_function_scales_out_then_back_to_minimum(
    e2e_cluster: E2ECluster,
    function_factory: FunctionFactory,
    router_url: str,
) -> None:
    """Sustained real requests expand a newdeploy Function, then it contracts."""
    worker_cpu = _largest_worker_cpu(e2e_cluster)
    function = function_factory(
        cpu_millicores=max(worker_cpu // 2, 500),
        minimum=1,
        target_cpu=20,
        route=True,
    )
    maximum = _wait_for_calculated_maximum(e2e_cluster, function, below=1_000)
    assert maximum >= 2
    assert _function_annotation(e2e_cluster, function, 'ready-nodes') == '3'

    deployment = _wait_for_function_deployment(e2e_cluster, function.name)
    hpa = _wait_for_function_hpa(e2e_cluster, deployment)
    _wait_for_hpa_maximum(e2e_cluster, hpa, maximum)
    _shorten_hpa_scale_down(e2e_cluster, hpa)
    protected_before = _protected_snapshot(e2e_cluster)

    assert function.path is not None
    with _LoadGenerator(f'{router_url}{function.path}', concurrency=max(maximum * 3, 12)) as load:
        peak = e2e_cluster.wait_for(
            'all capacity-limited Fission replicas to become Ready',
            lambda: maximum if _ready_replicas(e2e_cluster, deployment) == maximum else None,
            timeout=300,
            interval=5,
        )
        assert peak == maximum
        e2e_cluster.wait_for(
            'successful lambda responses',
            lambda: load.successes if load.successes >= 4 else None,
            timeout=60,
            interval=1,
        )

    e2e_cluster.wait_for(
        'Fission scale-down to MinScale=1',
        lambda: True if _ready_replicas(e2e_cluster, deployment) == 1 else None,
        timeout=300,
        interval=5,
    )
    _assert_runtime_priority(e2e_cluster, function.name)
    _assert_protected_unchanged(e2e_cluster, protected_before)


def test_capacity_contracts_while_ordinary_work_runs_and_recovers_afterward(
    e2e_cluster: E2ECluster,
    function_factory: FunctionFactory,
) -> None:
    """Reserved worker capacity lowers MaxScale and releasing it restores MaxScale."""
    function = function_factory(cpu_millicores=500)
    baseline = _wait_for_calculated_maximum(e2e_cluster, function, below=1_000)
    assert baseline >= 3
    protected_before = _protected_snapshot(e2e_cluster)

    _apply_reservation(e2e_cluster)
    try:
        e2e_cluster.kubectl(
            '-n',
            'default',
            'rollout',
            'status',
            'daemonset/autofission-e2e-reservation',
            '--timeout=5m',
        )
        contracted = e2e_cluster.wait_for(
            'MaxScale contraction after ordinary Pods reserve resources',
            lambda: (
                value if (value := _function_maximum(e2e_cluster, function)) < baseline else None
            ),
            timeout=120,
        )
        assert 1 <= contracted < baseline
    finally:
        e2e_cluster.kubectl(
            '-n',
            'default',
            'delete',
            'daemonset/autofission-e2e-reservation',
            '--ignore-not-found',
            '--wait=true',
        )

    e2e_cluster.wait_for(
        'MaxScale recovery after reservation Pods disappear',
        lambda: baseline if _function_maximum(e2e_cluster, function) == baseline else None,
        timeout=120,
    )
    _assert_protected_unchanged(e2e_cluster, protected_before)


def test_oversized_elastic_work_stays_pending_without_preempting_services(
    e2e_cluster: E2ECluster,
    function_factory: FunctionFactory,
) -> None:
    """Impossible demand is bounded and non-preempting priority preserves existing Pods."""
    protected_before = _protected_snapshot(e2e_cluster)
    preempted_before = _preempted_event_uids(e2e_cluster)
    worker_cpu = _largest_worker_cpu(e2e_cluster)
    function = function_factory(cpu_millicores=worker_cpu + 1_000, minimum=1)

    assert _wait_for_calculated_maximum(e2e_cluster, function, below=1_000) == 1
    _wait_for_function_deployment(e2e_cluster, function.name)
    pending_pod = e2e_cluster.wait_for(
        'oversized real Fission runtime Pod to remain Pending',
        lambda: _pending_function_pod(e2e_cluster, function.name),
        timeout=180,
        interval=3,
    )
    assert _pod_priority(pending_pod) == 'autofission-runtime'
    assert _pod_nominated_node(pending_pod) is None

    _apply_impossible_controller_priority_pod(e2e_cluster, worker_cpu)
    try:
        controller_pod = e2e_cluster.wait_for(
            'non-preempting high-priority Pod to remain Pending',
            lambda: _pending_named_pod(e2e_cluster, 'autofission-e2e-nonpreempting'),
            timeout=120,
        )
        assert _pod_priority(controller_pod) == 'autofission-controller'
        assert _pod_nominated_node(controller_pod) is None
        _wait_for_failed_scheduling(e2e_cluster, 'autofission-e2e-nonpreempting')
        _assert_priority_classes_never_preempt(e2e_cluster)
        _assert_protected_unchanged(e2e_cluster, protected_before)
        assert _preempted_event_uids(e2e_cluster) == preempted_before
    finally:
        e2e_cluster.kubectl(
            '-n',
            'default',
            'delete',
            'pod/autofission-e2e-nonpreempting',
            '--ignore-not-found',
            '--wait=true',
        )


def _function_document(cluster: E2ECluster, function: FissionFunction) -> Mapping[str, object]:
    return cluster.kubectl_json(
        '-n',
        'default',
        'get',
        'functions.fission.io',
        function.name,
    )


def _function_maximum(cluster: E2ECluster, function: FissionFunction) -> int:
    document = _function_document(cluster, function)
    spec = _mapping(document.get('spec'), 'function.spec')
    invoke = _mapping(spec.get('InvokeStrategy'), 'function.spec.InvokeStrategy')
    execution = _mapping(invoke.get('ExecutionStrategy'), 'ExecutionStrategy')
    maximum = execution.get('MaxScale')
    if isinstance(maximum, bool) or not isinstance(maximum, int):
        raise TypeError('Function MaxScale must be an integer')
    return maximum


def _function_annotation(cluster: E2ECluster, function: FissionFunction, name: str) -> str:
    metadata = _mapping(_function_document(cluster, function).get('metadata'), 'metadata')
    annotations = _mapping(metadata.get('annotations'), 'metadata.annotations')
    value = annotations.get(f'{ANNOTATION_PREFIX}/{name}')
    if not isinstance(value, str):
        raise TypeError(f'annotation {name!r} must be a string')
    return value


def _wait_for_calculated_maximum(
    cluster: E2ECluster,
    function: FissionFunction,
    *,
    below: int,
) -> int:
    def calculated() -> int | None:
        maximum = _function_maximum(cluster, function)
        annotation = _function_annotation(cluster, function, 'calculated-maxscale')
        return maximum if maximum < below and annotation == str(maximum) else None

    return cluster.wait_for('Autofission to calculate Function MaxScale', calculated, timeout=180)


def _all_workloads(cluster: E2ECluster, kind: str) -> list[_Workload]:
    document = cluster.kubectl_json('get', kind, '--all-namespaces')
    workloads: list[_Workload] = []
    for item in _items(document, kind):
        metadata = _mapping(item.get('metadata'), f'{kind}.metadata')
        workloads.append(
            _Workload(
                namespace=_string(metadata.get('namespace'), f'{kind}.namespace'),
                name=_string(metadata.get('name'), f'{kind}.name'),
                document=item,
            ),
        )
    return workloads


def _wait_for_function_deployment(cluster: E2ECluster, name: str) -> _Workload:
    def find() -> _Workload | None:
        matches = []
        for workload in _all_workloads(cluster, 'deployments'):
            metadata = _mapping(workload.document.get('metadata'), 'deployment.metadata')
            labels = _mapping(metadata.get('labels', {}), 'deployment.metadata.labels')
            if labels.get('functionName') == name:
                matches.append(workload)
        if len(matches) > 1:
            raise AssertionError(f'multiple Deployments found for Function {name}')
        return matches[0] if matches else None

    return cluster.wait_for(f'Deployment for Function {name}', find, timeout=180)


def _wait_for_function_hpa(cluster: E2ECluster, deployment: _Workload) -> _Workload:
    def find() -> _Workload | None:
        matches = []
        for workload in _all_workloads(cluster, 'hpa'):
            spec = _mapping(workload.document.get('spec'), 'hpa.spec')
            target = _mapping(spec.get('scaleTargetRef'), 'hpa.spec.scaleTargetRef')
            if target.get('name') == deployment.name and workload.namespace == deployment.namespace:
                matches.append(workload)
        if len(matches) > 1:
            raise AssertionError(f'multiple HPAs target Deployment {deployment.name}')
        return matches[0] if matches else None

    return cluster.wait_for(f'HPA for Deployment {deployment.name}', find, timeout=180)


def _wait_for_hpa_maximum(cluster: E2ECluster, hpa: _Workload, expected: int) -> None:
    def converged() -> bool | None:
        current = cluster.kubectl_json('-n', hpa.namespace, 'get', 'hpa', hpa.name)
        spec = _mapping(current.get('spec'), 'hpa.spec')
        return True if spec.get('maxReplicas') == expected else None

    cluster.wait_for('Fission to propagate MaxScale to HPA', converged, timeout=180)


def _shorten_hpa_scale_down(cluster: E2ECluster, hpa: _Workload) -> None:
    patch = json.dumps(
        {
            'spec': {
                'behavior': {
                    'scaleUp': {'stabilizationWindowSeconds': 0},
                    'scaleDown': {'stabilizationWindowSeconds': 15},
                },
            },
        },
    )
    cluster.kubectl(
        '-n',
        hpa.namespace,
        'patch',
        'hpa',
        hpa.name,
        '--type=merge',
        '--patch',
        patch,
    )


def _ready_replicas(cluster: E2ECluster, deployment: _Workload) -> int:
    document = cluster.kubectl_json(
        '-n',
        deployment.namespace,
        'get',
        'deployment',
        deployment.name,
    )
    status = _mapping(document.get('status', {}), 'deployment.status')
    value = status.get('readyReplicas', 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('deployment.status.readyReplicas must be an integer')
    return value


def _assert_runtime_priority(cluster: E2ECluster, function_name: str) -> None:
    pods = _function_pods(cluster, function_name)
    assert pods
    assert all(_pod_priority(pod) == 'autofission-runtime' for pod in pods)


def _function_pods(cluster: E2ECluster, function_name: str) -> list[Mapping[str, object]]:
    document = cluster.kubectl_json(
        'get',
        'pods',
        '--all-namespaces',
        '-l',
        f'functionName={function_name}',
    )
    return _items(document, 'function pods')


def _pending_function_pod(
    cluster: E2ECluster,
    function_name: str,
) -> Mapping[str, object] | None:
    pending = [
        pod for pod in _function_pods(cluster, function_name) if _pod_phase(pod) == 'Pending'
    ]
    if len(pending) > 1:
        raise AssertionError(f'expected at most one oversized Pod, got {len(pending)}')
    return pending[0] if pending else None


def _protected_snapshot(cluster: E2ECluster) -> dict[str, tuple[str, str, int]]:
    document = cluster.kubectl_json(
        '-n',
        'default',
        'get',
        'pods',
        '-l',
        PROTECTED_LABEL,
    )
    result: dict[str, tuple[str, str, int]] = {}
    for pod in _items(document, 'protected pods'):
        metadata = _mapping(pod.get('metadata'), 'protected Pod metadata')
        spec = _mapping(pod.get('spec'), 'protected Pod spec')
        status = _mapping(pod.get('status'), 'protected Pod status')
        name = _string(metadata.get('name'), 'protected Pod name')
        uid = _string(metadata.get('uid'), 'protected Pod UID')
        node = _string(spec.get('nodeName'), 'protected Pod nodeName')
        restart_count = 0
        if status.get('phase') != 'Running' or not _pod_is_ready(status):
            raise AssertionError(f'protected Pod {name} is not Ready')
        statuses = status.get('containerStatuses', [])
        if not isinstance(statuses, list):
            raise TypeError('protected Pod containerStatuses must be a list')
        for container_status in statuses:
            value = _mapping(container_status, 'container status').get('restartCount')
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError('container restartCount must be an integer')
            restart_count += value
        result[name] = (uid, node, restart_count)
    if len(result) != 3:
        raise AssertionError(f'expected three protected Pods, got {len(result)}')
    return result


def _assert_protected_unchanged(
    cluster: E2ECluster,
    expected: Mapping[str, tuple[str, str, int]],
) -> None:
    assert _protected_snapshot(cluster) == expected


def _apply_reservation(cluster: E2ECluster) -> None:
    cluster.apply(
        {
            'apiVersion': 'apps/v1',
            'kind': 'DaemonSet',
            'metadata': {'name': 'autofission-e2e-reservation', 'namespace': 'default'},
            'spec': {
                'selector': {'matchLabels': {'app': 'autofission-e2e-reservation'}},
                'template': {
                    'metadata': {'labels': {'app': 'autofission-e2e-reservation'}},
                    'spec': {
                        'nodeSelector': {'autofission.io/e2e-worker': 'true'},
                        'containers': [
                            {
                                'name': 'reservation',
                                'image': 'registry.k8s.io/pause:3.10',
                                'resources': {
                                    'requests': {'cpu': '750m', 'memory': '64Mi'},
                                    'limits': {'cpu': '750m', 'memory': '64Mi'},
                                },
                            },
                        ],
                    },
                },
            },
        },
    )


def _largest_worker_cpu(cluster: E2ECluster) -> int:
    document = cluster.kubectl_json('get', 'nodes', '-l', WORKER_LABEL)
    capacities = []
    for node in _items(document, 'worker nodes'):
        status = _mapping(node.get('status'), 'node.status')
        allocatable = _mapping(status.get('allocatable'), 'node.status.allocatable')
        capacities.append(parse_cpu_millicores(_string(allocatable.get('cpu'), 'allocatable.cpu')))
    if len(capacities) != 3:
        raise AssertionError(f'expected three worker capacities, got {len(capacities)}')
    return max(capacities)


def _apply_impossible_controller_priority_pod(cluster: E2ECluster, cpu: int) -> None:
    cluster.apply(
        {
            'apiVersion': 'v1',
            'kind': 'Pod',
            'metadata': {'name': 'autofission-e2e-nonpreempting', 'namespace': 'default'},
            'spec': {
                'priorityClassName': 'autofission-controller',
                'nodeSelector': {'autofission.io/e2e-worker': 'true'},
                'containers': [
                    {
                        'name': 'impossible',
                        'image': 'registry.k8s.io/pause:3.10',
                        'resources': {
                            'requests': {'cpu': f'{cpu}m', 'memory': '32Mi'},
                            'limits': {'cpu': f'{cpu}m', 'memory': '32Mi'},
                        },
                    },
                ],
            },
        },
    )


def _pending_named_pod(cluster: E2ECluster, name: str) -> Mapping[str, object] | None:
    pod = cluster.kubectl_json('-n', 'default', 'get', 'pod', name)
    return pod if _pod_phase(pod) == 'Pending' else None


def _wait_for_failed_scheduling(cluster: E2ECluster, pod_name: str) -> None:
    def found() -> bool | None:
        document = cluster.kubectl_json(
            '-n',
            'default',
            'get',
            'events',
            '--field-selector',
            f'involvedObject.kind=Pod,involvedObject.name={pod_name},reason=FailedScheduling',
        )
        return True if _items(document, 'FailedScheduling events') else None

    cluster.wait_for(f'FailedScheduling event for {pod_name}', found, timeout=120)


def _assert_priority_classes_never_preempt(cluster: E2ECluster) -> None:
    for name in ('autofission-controller', 'autofission-runtime'):
        priority = cluster.kubectl_json('get', 'priorityclass', name)
        assert priority.get('preemptionPolicy') == 'Never'


def _preempted_event_uids(cluster: E2ECluster) -> set[str]:
    document = cluster.kubectl_json(
        'get',
        'events',
        '--all-namespaces',
        '--field-selector',
        'reason=Preempted',
    )
    result = set()
    for event in _items(document, 'Preempted events'):
        metadata = _mapping(event.get('metadata'), 'event.metadata')
        result.add(_string(metadata.get('uid'), 'event.metadata.uid'))
    return result


def _pod_priority(pod: Mapping[str, object]) -> object:
    return _mapping(pod.get('spec'), 'pod.spec').get('priorityClassName')


def _pod_phase(pod: Mapping[str, object]) -> object:
    return _mapping(pod.get('status'), 'pod.status').get('phase')


def _pod_nominated_node(pod: Mapping[str, object]) -> object:
    return _mapping(pod.get('status'), 'pod.status').get('nominatedNodeName')


def _pod_is_ready(status: Mapping[str, object]) -> bool:
    conditions = status.get('conditions')
    if not isinstance(conditions, list):
        return False
    return any(
        _mapping(condition, 'pod condition').get('type') == 'Ready'
        and _mapping(condition, 'pod condition').get('status') == 'True'
        for condition in conditions
    )


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
