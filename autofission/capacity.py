"""Scheduler-aware cluster and function capacity calculations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from autofission.errors import CapacityError
from autofission.models import NodeResources, Resources
from autofission.quantities import (
    parse_cpu_millicores,
    parse_memory_bytes,
    parse_pod_count,
)

JsonObject: TypeAlias = Mapping[str, object]

_TERMINAL_PHASES = frozenset({'Succeeded', 'Failed'})
_BLOCKING_TAINT_EFFECTS = frozenset({'NoSchedule', 'NoExecute'})
_FUNCTION_UID_LABEL = 'functionUid'


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise CapacityError(f'{context} must be an object')
    return value


def _objects(value: object, context: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise CapacityError(f'{context} must be a list')
    return value


def _name(metadata: JsonObject, context: str) -> str:
    value = metadata.get('name')
    if not isinstance(value, str) or not value:
        raise CapacityError(f'{context}.metadata.name must be a non-empty string')
    return value


def _quantity(requests: JsonObject, name: str, parser: object) -> int:
    value = requests.get(name)
    if value is None:
        return 0
    if not isinstance(value, str):
        raise CapacityError(f'resource request {name!r} must be a string')
    if parser is parse_cpu_millicores:
        return parse_cpu_millicores(value)
    return parse_memory_bytes(value)


def _request_resources(resources: object) -> Resources:
    if resources is None:
        return Resources()
    resource_object = _object(resources, 'resources')
    requests_value = resource_object.get('requests', {})
    if requests_value is None:
        requests_value = {}
    requests = _object(requests_value, 'resources.requests')
    return Resources(
        _quantity(requests, 'cpu', parse_cpu_millicores),
        _quantity(requests, 'memory', parse_memory_bytes),
    )


def _optional_resource(
    resources: JsonObject,
    section: str,
    name: str,
) -> int | None:
    section_value = resources.get(section, {})
    if section_value is None:
        section_value = {}
    values = _object(section_value, f'resources.{section}')
    if name not in values:
        return None
    value = values[name]
    if not isinstance(value, str):
        raise CapacityError(f'{section} {name} quantity must be a string')
    parser = parse_cpu_millicores if name == 'cpu' else parse_memory_bytes
    return parser(value)


def _defaulted_request(
    environment_resources: JsonObject,
    function_resources: JsonObject,
    name: str,
) -> int:
    request = _optional_resource(environment_resources, 'requests', name)
    limit = _optional_resource(environment_resources, 'limits', name)
    function_request = _optional_resource(function_resources, 'requests', name)
    function_limit = _optional_resource(function_resources, 'limits', name)
    if function_request:
        request = function_request
    if function_limit:
        limit = function_limit
    if request is None and limit is not None:
        request = limit
    return 0 if request is None else request


def _container_requests(container: object) -> Resources:
    container_object = _object(container, 'container')
    return _request_resources(container_object.get('resources'))


def pod_requests(spec: object) -> Resources:
    """Calculate effective CPU/memory requests using scheduler semantics."""
    pod_spec = _object(spec, 'pod.spec')
    containers = _objects(pod_spec.get('containers'), 'pod.spec.containers')
    if not containers:
        raise CapacityError('pod.spec.containers cannot be empty')

    regular = sum(
        (_container_requests(container) for container in containers),
        start=Resources(),
    )
    restartable_init = Resources()
    init_peak = Resources()
    init_containers_value = pod_spec.get('initContainers', [])
    if init_containers_value is None:
        init_containers_value = []
    for container in _objects(init_containers_value, 'pod.spec.initContainers'):
        container_object = _object(container, 'init container')
        requests = _container_requests(container_object)
        restart_policy = container_object.get('restartPolicy')
        if restart_policy == 'Always':
            restartable_init += requests
            init_peak = init_peak.maximum(restartable_init)
        elif restart_policy is None:
            init_peak = init_peak.maximum(restartable_init + requests)
        else:
            raise CapacityError('init container restartPolicy must be "Always" or absent')

    effective = (regular + restartable_init).maximum(init_peak)
    effective = effective.maximum(_request_resources(pod_spec.get('resources')))

    overhead_value = pod_spec.get('overhead')
    if overhead_value is not None:
        overhead = _object(overhead_value, 'pod.spec.overhead')
        effective += Resources(
            _quantity(overhead, 'cpu', parse_cpu_millicores),
            _quantity(overhead, 'memory', parse_memory_bytes),
        )
    return effective


def _node_resources(node: object, *, include_tainted: bool) -> NodeResources | None:
    node_object = _object(node, 'node')
    metadata = _object(node_object.get('metadata'), 'node.metadata')
    spec = _object(node_object.get('spec', {}), 'node.spec')
    status = _object(node_object.get('status', {}), 'node.status')

    if metadata.get('deletionTimestamp') is not None:
        return None
    unschedulable = spec.get('unschedulable', False)
    if not isinstance(unschedulable, bool):
        raise CapacityError('node.spec.unschedulable must be a boolean')
    if unschedulable:
        return None

    conditions = _objects(status.get('conditions', []), 'node.status.conditions')
    ready_statuses: set[str] = set()
    for condition in conditions:
        condition_object = _object(condition, 'node condition')
        condition_type = condition_object.get('type')
        condition_status = condition_object.get('status')
        if not isinstance(condition_type, str) or not isinstance(condition_status, str):
            raise CapacityError('node condition type and status must be strings')
        if condition_type == 'Ready':
            ready_statuses.add(condition_status)
    if len(ready_statuses) > 1:
        raise CapacityError('node has contradictory Ready conditions')
    if ready_statuses != {'True'}:
        return None

    taints_value = spec.get('taints', [])
    if taints_value is None:
        taints_value = []
    taints = _objects(taints_value, 'node.spec.taints')
    for taint in taints:
        effect = _object(taint, 'node taint').get('effect')
        if not isinstance(effect, str):
            raise CapacityError('node taint effect must be a string')
        if not include_tainted and effect in _BLOCKING_TAINT_EFFECTS:
            return None

    allocatable = _object(status.get('allocatable'), 'node.status.allocatable')
    cpu = allocatable.get('cpu')
    memory = allocatable.get('memory')
    pods = allocatable.get('pods')
    if not isinstance(cpu, str) or not isinstance(memory, str) or not isinstance(pods, str):
        raise CapacityError('node allocatable cpu, memory, and pods must be strings')
    return NodeResources(
        name=_name(metadata, 'node'),
        allocatable=Resources(
            parse_cpu_millicores(cpu),
            parse_memory_bytes(memory),
        ),
        pod_slots=parse_pod_count(pods),
    )


@dataclass(frozen=True, slots=True)
class _PodAllocation:
    node_name: str
    resources: Resources
    function_uid: str | None


def _pod_allocation(pod: object) -> _PodAllocation | None:
    pod_object = _object(pod, 'pod')
    spec = _object(pod_object.get('spec'), 'pod.spec')
    status = _object(pod_object.get('status', {}), 'pod.status')
    phase = status.get('phase')
    if phase in _TERMINAL_PHASES:
        return None
    if phase is not None and not isinstance(phase, str):
        raise CapacityError('pod.status.phase must be a string')

    node_name = spec.get('nodeName')
    if node_name in (None, ''):
        return None
    if not isinstance(node_name, str):
        raise CapacityError('pod.spec.nodeName must be a string')

    metadata = _object(pod_object.get('metadata', {}), 'pod.metadata')
    labels_value = metadata.get('labels')
    function_uid: str | None = None
    if labels_value is not None:
        labels = _object(labels_value, 'pod.metadata.labels')
        uid_value = labels.get(_FUNCTION_UID_LABEL)
        if uid_value is not None and not isinstance(uid_value, str):
            raise CapacityError('pod functionUid label must be a string')
        function_uid = uid_value
    return _PodAllocation(node_name, pod_requests(spec), function_uid)


class ClusterSnapshot:
    """Immutable per-node usage snapshot used for several function plans."""

    def __init__(
        self,
        nodes: Mapping[str, NodeResources],
        used: Mapping[str, Resources],
        used_slots: Mapping[str, int],
        function_used: Mapping[str, Mapping[str, Resources]],
        function_slots: Mapping[str, Mapping[str, int]],
        observed_requests: Mapping[str, Resources],
    ) -> None:
        self._nodes = dict(nodes)
        self._used = dict(used)
        self._used_slots = dict(used_slots)
        self._function_used = {uid: dict(resources) for uid, resources in function_used.items()}
        self._function_slots = {uid: dict(slots) for uid, slots in function_slots.items()}
        self._observed_requests = dict(observed_requests)

    @classmethod
    def build(
        cls,
        nodes: Iterable[object],
        pods: Iterable[object],
        *,
        include_tainted: bool = False,
    ) -> ClusterSnapshot:
        """Validate API objects and build a capacity snapshot."""
        node_map: dict[str, NodeResources] = {}
        for node in nodes:
            parsed = _node_resources(node, include_tainted=include_tainted)
            if parsed is None:
                continue
            if parsed.name in node_map:
                raise CapacityError(f'duplicate node name: {parsed.name!r}')
            node_map[parsed.name] = parsed
        if not node_map:
            raise CapacityError('no schedulable Ready nodes')

        used: defaultdict[str, Resources] = defaultdict(Resources)
        used_slots: defaultdict[str, int] = defaultdict(int)
        function_used: defaultdict[str, defaultdict[str, Resources]] = defaultdict(
            lambda: defaultdict(Resources),
        )
        function_slots: defaultdict[str, defaultdict[str, int]] = defaultdict(
            lambda: defaultdict(int),
        )
        observed: defaultdict[str, Resources] = defaultdict(Resources)

        for pod in pods:
            allocation = _pod_allocation(pod)
            if allocation is None or allocation.node_name not in node_map:
                continue
            used[allocation.node_name] += allocation.resources
            used_slots[allocation.node_name] += 1
            if allocation.function_uid:
                uid = allocation.function_uid
                function_used[uid][allocation.node_name] += allocation.resources
                function_slots[uid][allocation.node_name] += 1
                observed[uid] = observed[uid].maximum(allocation.resources)

        return cls(
            node_map,
            used,
            used_slots,
            function_used,
            function_slots,
            observed,
        )

    @property
    def ready_nodes(self) -> int:
        """Number of Ready, schedulable nodes in the snapshot."""
        return len(self._nodes)

    def observed_request(self, function_uid: str) -> Resources:
        """Largest request vector observed on an existing function Pod."""
        return self._observed_requests.get(function_uid, Resources())

    def function_capacity(self, function_uid: str, request: Resources) -> int:
        """Return the total per-node bin-packed capacity for one function."""
        if not function_uid:
            raise CapacityError('function UID must be a non-empty string')
        if request.cpu_millicores <= 0 or request.memory_bytes <= 0:
            raise CapacityError('managed functions need positive CPU and memory requests')

        total = 0
        own_used = self._function_used.get(function_uid, {})
        own_slots = self._function_slots.get(function_uid, {})
        for name, node in self._nodes.items():
            used = self._used.get(name, Resources()) - own_used.get(name, Resources())
            slots = self._used_slots.get(name, 0) - own_slots.get(name, 0)
            free_cpu = max(node.allocatable.cpu_millicores - used.cpu_millicores, 0)
            free_memory = max(node.allocatable.memory_bytes - used.memory_bytes, 0)
            free_slots = max(node.pod_slots - slots, 0)
            total += min(
                free_cpu // request.cpu_millicores,
                free_memory // request.memory_bytes,
                free_slots,
            )
        return total


def index_environments(environments: Iterable[object]) -> dict[tuple[str, str], JsonObject]:
    """Index Fission Environments by namespace and name."""
    result: dict[tuple[str, str], JsonObject] = {}
    for environment in environments:
        environment_object = _object(environment, 'environment')
        metadata = _object(environment_object.get('metadata'), 'environment.metadata')
        namespace = metadata.get('namespace')
        if not isinstance(namespace, str) or not namespace:
            raise CapacityError('environment.metadata.namespace must be a string')
        key = (namespace, _name(metadata, 'environment'))
        if key in result:
            raise CapacityError(f'duplicate environment: {namespace}/{key[1]}')
        result[key] = environment_object
    return result


def function_request(
    function: object,
    environments: Mapping[tuple[str, str], JsonObject],
    fetcher: Resources,
) -> Resources:
    """Resolve Function request overrides over Environment defaults."""
    function_object = _object(function, 'function')
    metadata = _object(function_object.get('metadata'), 'function.metadata')
    function_namespace = metadata.get('namespace')
    if not isinstance(function_namespace, str) or not function_namespace:
        raise CapacityError('function.metadata.namespace must be a string')
    spec = _object(function_object.get('spec'), 'function.spec')
    reference = _object(spec.get('environment'), 'function.spec.environment')
    environment_name = reference.get('name')
    environment_namespace = reference.get('namespace') or function_namespace
    if not isinstance(environment_name, str) or not environment_name:
        raise CapacityError('function environment name must be a string')
    if not isinstance(environment_namespace, str):
        raise CapacityError('function environment namespace must be a string')

    key = (environment_namespace, environment_name)
    try:
        environment = environments[key]
    except KeyError as error:
        raise CapacityError(
            f'environment {environment_namespace}/{environment_name} was not found',
        ) from error

    environment_spec = _object(environment.get('spec'), 'environment.spec')
    environment_resources = _object(
        environment_spec.get('resources', {}),
        'environment.spec.resources',
    )
    function_resources = _object(spec.get('resources', {}), 'function.spec.resources')

    main = Resources(
        _defaulted_request(environment_resources, function_resources, 'cpu'),
        _defaulted_request(environment_resources, function_resources, 'memory'),
    )
    if main.cpu_millicores <= 0 or main.memory_bytes <= 0:
        raise CapacityError('resolved function container requests must be positive')
    return main + fetcher
