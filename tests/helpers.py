"""Factories and fakes shared by tests."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy


def node(
    name: str = 'node-a',
    cpu: str = '4',
    memory: str = '8Gi',
    pods: str = '110',
    *,
    ready: str = 'True',
    unschedulable: bool = False,
    deleting: bool = False,
    taints: list[object] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {'name': name}
    if deleting:
        metadata['deletionTimestamp'] = '2026-01-01T00:00:00Z'
    return {
        'metadata': metadata,
        'spec': {
            'unschedulable': unschedulable,
            'taints': [] if taints is None else taints,
        },
        'status': {
            'allocatable': {'cpu': cpu, 'memory': memory, 'pods': pods},
            'conditions': [{'type': 'Ready', 'status': ready}],
        },
    }


def container(
    cpu: str | None = '100m',
    memory: str | None = '64Mi',
    **extra: object,
) -> dict[str, object]:
    requests: dict[str, str] = {}
    if cpu is not None:
        requests['cpu'] = cpu
    if memory is not None:
        requests['memory'] = memory
    result: dict[str, object] = {'resources': {'requests': requests}}
    result.update(extra)
    return result


def pod(
    name: str = 'pod-a',
    node_name: str | None = 'node-a',
    cpu: str = '100m',
    memory: str = '64Mi',
    *,
    phase: object = 'Running',
    function_uid: object = None,
    extra_spec: Mapping[str, object] | None = None,
) -> dict[str, object]:
    labels: dict[str, object] = {}
    if function_uid is not None:
        labels['functionUid'] = function_uid
    spec: dict[str, object] = {
        'containers': [container(cpu, memory)],
    }
    if node_name is not None:
        spec['nodeName'] = node_name
    if extra_spec:
        spec.update(deepcopy(dict(extra_spec)))
    return {
        'metadata': {'name': name, 'labels': labels},
        'spec': spec,
        'status': {'phase': phase},
    }


def environment(
    name: str = 'python',
    namespace: str = 'fission-function',
    cpu: str | None = '250m',
    memory: str | None = '128Mi',
    cpu_limit: str | None = None,
    memory_limit: str | None = None,
) -> dict[str, object]:
    requests: dict[str, str] = {}
    limits: dict[str, str] = {}
    if cpu is not None:
        requests['cpu'] = cpu
    if memory is not None:
        requests['memory'] = memory
    if cpu_limit is not None:
        limits['cpu'] = cpu_limit
    if memory_limit is not None:
        limits['memory'] = memory_limit
    return {
        'metadata': {'name': name, 'namespace': namespace},
        'spec': {'resources': {'requests': requests, 'limits': limits}},
    }


def function(
    name: str = 'hello',
    namespace: str = 'fission-function',
    uid: str = 'uid-1',
    *,
    resource_version: str = '7',
    cpu: str | None = '250m',
    memory: str | None = '128Mi',
    cpu_limit: str | None = None,
    memory_limit: str | None = None,
    minimum: object = 0,
    maximum: object = 2,
    executor: object = 'newdeploy',
    managed: bool = True,
    annotations: Mapping[str, object] | None = None,
    environment_name: str = 'python',
    environment_namespace: str = 'fission-function',
) -> dict[str, object]:
    requests: dict[str, str] = {}
    limits: dict[str, str] = {}
    if cpu is not None:
        requests['cpu'] = cpu
    if memory is not None:
        requests['memory'] = memory
    if cpu_limit is not None:
        limits['cpu'] = cpu_limit
    if memory_limit is not None:
        limits['memory'] = memory_limit
    labels = {'autoscaling.fission.io/cluster-capacity': 'true'} if managed else {}
    return {
        'metadata': {
            'name': name,
            'namespace': namespace,
            'uid': uid,
            'resourceVersion': resource_version,
            'labels': labels,
            'annotations': {} if annotations is None else dict(annotations),
        },
        'spec': {
            'environment': {
                'name': environment_name,
                'namespace': environment_namespace,
            },
            'resources': {'requests': requests, 'limits': limits},
            'InvokeStrategy': {
                'ExecutionStrategy': {
                    'ExecutorType': executor,
                    'MinScale': minimum,
                    'MaxScale': maximum,
                },
            },
        },
    }
