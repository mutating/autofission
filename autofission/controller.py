"""Fission reconciliation logic."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Mapping as TypingMapping
from typing import Protocol

from autofission.capacity import (
    ClusterSnapshot,
    function_request,
    index_environments,
)
from autofission.errors import CapacityError, ReconcileError
from autofission.models import ReconcileResult, Resources

JsonObject = TypingMapping[str, object]

MANAGED_LABEL = 'autoscaling.fission.io/cluster-capacity'
MANAGED_VALUE = 'true'
ANNOTATION_PREFIX = 'autoscaling.fission.io'
INT32_MAX = 2**31 - 1
_TERMINAL_POD_PHASES = frozenset({'Succeeded', 'Failed'})
_FUNCTION_UID_LABEL = 'functionUid'


class KubernetesGateway(Protocol):
    """Minimal Kubernetes operations required by the controller."""

    def list_nodes(self) -> list[object]: ...

    def list_pods(self) -> list[object]: ...

    def list_functions(self, label_selector: str) -> list[object]: ...

    def list_environments(self) -> list[object]: ...

    def patch_function(
        self,
        namespace: str,
        name: str,
        body: JsonObject,
    ) -> object: ...


@dataclass(frozen=True)
class ControllerConfig:
    """Inputs that affect capacity and opt-in selection."""

    fetcher_request: Resources = field(
        default_factory=lambda: Resources(10, 16 * 2**20),
    )
    managed_label: str = MANAGED_LABEL
    managed_value: str = MANAGED_VALUE
    include_tainted_nodes: bool = False
    runtime_priority_class: str | None = None

    def __post_init__(self) -> None:
        if self.fetcher_request.cpu_millicores < 0:
            raise ValueError('fetcher CPU request cannot be negative')
        if self.fetcher_request.memory_bytes < 0:
            raise ValueError('fetcher memory request cannot be negative')
        if not self.managed_label or not self.managed_value:
            raise ValueError('managed label and value cannot be empty')
        if self.runtime_priority_class is not None and not self.runtime_priority_class:
            raise ValueError('runtime priority class cannot be empty')


@dataclass(frozen=True)
class _FunctionIdentity:
    namespace: str
    name: str
    uid: str
    resource_version: str

    @property
    def display_name(self) -> str:
        return f'{self.namespace}/{self.name}'


@dataclass(frozen=True)
class _FunctionPod:
    display_name: str
    priority_class: str | None


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise CapacityError(f'{context} must be an object')
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise CapacityError(f'{context} must be a non-empty string')
    return value


def _integer(value: object, context: str, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapacityError(f'{context} must be an integer')
    return value


def _metadata(function: object) -> JsonObject:
    return _object(_object(function, 'function').get('metadata'), 'function.metadata')


def _is_managed(function: object, config: ControllerConfig) -> bool:
    metadata = _metadata(function)
    labels_value = metadata.get('labels')
    if labels_value is None:
        return False
    labels = _object(labels_value, 'function.metadata.labels')
    return labels.get(config.managed_label) == config.managed_value


def _identity(metadata: JsonObject) -> _FunctionIdentity:
    return _FunctionIdentity(
        namespace=_string(metadata.get('namespace'), 'function namespace'),
        name=_string(metadata.get('name'), 'function name'),
        uid=_string(metadata.get('uid'), 'function UID'),
        resource_version=_string(
            metadata.get('resourceVersion'),
            'function resourceVersion',
        ),
    )


def _execution(function: object) -> JsonObject:
    spec = _object(_object(function, 'function').get('spec'), 'function.spec')
    invoke = _object(spec.get('InvokeStrategy'), 'function.spec.InvokeStrategy')
    return _object(
        invoke.get('ExecutionStrategy'),
        'function.spec.InvokeStrategy.ExecutionStrategy',
    )


def _index_function_pods(pods: list[object]) -> dict[str, tuple[_FunctionPod, ...]]:
    indexed: defaultdict[str, list[_FunctionPod]] = defaultdict(list)
    for pod in pods:
        pod_object = _object(pod, 'pod')
        status = _object(pod_object.get('status', {}), 'pod.status')
        phase = status.get('phase')
        if phase in _TERMINAL_POD_PHASES:
            continue
        if phase is not None and not isinstance(phase, str):
            raise CapacityError('pod.status.phase must be a string')

        metadata = _object(pod_object.get('metadata', {}), 'pod.metadata')
        labels_value = metadata.get('labels')
        if labels_value is None:
            continue
        labels = _object(labels_value, 'pod.metadata.labels')
        uid = labels.get(_FUNCTION_UID_LABEL)
        if uid is None:
            continue
        if not isinstance(uid, str) or not uid:
            raise CapacityError('pod functionUid label must be a non-empty string')

        name = _string(metadata.get('name'), 'pod name')
        namespace_value = metadata.get('namespace')
        if namespace_value is None:
            display_name = name
        else:
            namespace = _string(namespace_value, 'pod namespace')
            display_name = f'{namespace}/{name}'

        spec = _object(pod_object.get('spec'), 'pod.spec')
        priority_class = spec.get('priorityClassName')
        if priority_class is not None and (
            not isinstance(priority_class, str) or not priority_class
        ):
            raise CapacityError('pod.spec.priorityClassName must be a non-empty string')
        indexed[uid].append(_FunctionPod(display_name, priority_class))
    return {uid: tuple(items) for uid, items in indexed.items()}


class Controller:
    """Reconcile opt-in Fission Functions against current free capacity."""

    def __init__(
        self,
        gateway: KubernetesGateway,
        config: ControllerConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._gateway = gateway
        self._config = config or ControllerConfig()
        self._logger = logger or logging.getLogger('autofission')

    def reconcile(self) -> ReconcileResult:
        """Run one complete pass, updating readiness only at the caller layer."""
        functions = self._gateway.list_functions(
            f'{self._config.managed_label}={self._config.managed_value}',
        )
        environments = index_environments(self._gateway.list_environments())
        pods = self._gateway.list_pods()
        function_pods = (
            _index_function_pods(pods) if self._config.runtime_priority_class is not None else {}
        )
        snapshot = ClusterSnapshot.build(
            self._gateway.list_nodes(),
            pods,
            include_tainted=self._config.include_tainted_nodes,
        )

        managed = 0
        updated = 0
        failures: list[str] = []
        for function in functions:
            try:
                if not _is_managed(function, self._config):
                    continue
                managed += 1
                if self._reconcile_function(
                    function,
                    environments,
                    snapshot,
                    function_pods,
                ):
                    updated += 1
            except Exception as error:  # Each malformed CR must be isolated.
                display_name = self._safe_display_name(function)
                failures.append(display_name)
                details = str(error)
                self._logger.exception(
                    'failed to reconcile %s: %s: %s',
                    display_name,
                    type(error).__name__,
                    details,
                )

        result = ReconcileResult(managed, updated, snapshot.ready_nodes)
        if failures:
            raise ReconcileError(tuple(failures))
        self._logger.info(
            'reconciled managed_functions=%d updated_functions=%d ready_nodes=%d',
            result.managed_functions,
            result.updated_functions,
            result.ready_nodes,
        )
        return result

    def _reconcile_function(
        self,
        function: object,
        environments: Mapping[tuple[str, str], JsonObject],
        snapshot: ClusterSnapshot,
        function_pods: Mapping[str, tuple[_FunctionPod, ...]],
    ) -> bool:
        metadata = _metadata(function)
        identity = _identity(metadata)
        if metadata.get('deletionTimestamp') is not None:
            self._logger.info('skip terminating function %s', identity.display_name)
            return False

        execution = _execution(function)
        executor_type = execution.get('ExecutorType')
        if executor_type != 'newdeploy':
            raise CapacityError(
                f'{identity.display_name} uses unsupported executor {executor_type!r}',
            )

        self._validate_runtime_priority_class(identity, function_pods)

        minimum = _integer(execution.get('MinScale'), 'MinScale', default=0)
        if minimum < 0 or minimum > INT32_MAX:
            raise CapacityError(f'MinScale must be between 0 and {INT32_MAX}')
        current_maximum = _integer(execution.get('MaxScale'), 'MaxScale')
        if current_maximum <= 0:
            raise CapacityError('MaxScale must be positive')

        configured_request = function_request(
            function,
            environments,
            self._config.fetcher_request,
        )
        request = configured_request.maximum(snapshot.observed_request(identity.uid))
        calculated = snapshot.function_capacity(identity.uid, request)
        maximum = min(max(calculated, minimum, 1), INT32_MAX)
        expected_annotations = {
            f'{ANNOTATION_PREFIX}/calculated-maxscale': str(maximum),
            f'{ANNOTATION_PREFIX}/ready-nodes': str(snapshot.ready_nodes),
        }

        annotations_value = metadata.get('annotations')
        annotations = (
            {}
            if annotations_value is None
            else _object(annotations_value, 'function.metadata.annotations')
        )
        annotations_match = all(
            annotations.get(key) == value for key, value in expected_annotations.items()
        )
        if current_maximum == maximum and annotations_match:
            return False

        self._gateway.patch_function(
            identity.namespace,
            identity.name,
            {
                'metadata': {
                    'resourceVersion': identity.resource_version,
                    'annotations': expected_annotations,
                },
                'spec': {
                    'InvokeStrategy': {
                        'ExecutionStrategy': {'MaxScale': maximum},
                    },
                },
            },
        )
        self._logger.info(
            'updated %s maxscale=%d ready_nodes=%d pod_cpu_m=%d pod_memory_bytes=%d',
            identity.display_name,
            maximum,
            snapshot.ready_nodes,
            request.cpu_millicores,
            request.memory_bytes,
        )
        return True

    def _validate_runtime_priority_class(
        self,
        identity: _FunctionIdentity,
        function_pods: Mapping[str, tuple[_FunctionPod, ...]],
    ) -> None:
        expected = self._config.runtime_priority_class
        if expected is None:
            return
        for pod in function_pods.get(identity.uid, ()):
            if pod.priority_class == expected:
                continue
            actual = pod.priority_class or '<none>'
            raise CapacityError(
                f'{identity.display_name} has Function Pod {pod.display_name} with '
                f'priorityClassName {actual!r}; expected {expected!r}. Configure Fission '
                'runtimePodSpec.podSpec.priorityClassName, restart the Fission executor, '
                'and recreate existing Function workloads before opting them in',
            )

    @staticmethod
    def _safe_display_name(function: object) -> str:
        try:
            metadata = _metadata(function)
            namespace = metadata.get('namespace', '?')
            name = metadata.get('name', '?')
        except CapacityError:
            return '?/?'
        return f'{namespace}/{name}'
