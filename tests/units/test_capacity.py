from __future__ import annotations

from copy import deepcopy

import pytest

from autofission.capacity import (
    ClusterSnapshot,
    function_request,
    index_environments,
    pod_requests,
)
from autofission.errors import CapacityError
from autofission.models import Resources
from tests.helpers import container, environment, function, node, pod


def test_pod_requests_sum_regular_containers_and_round_each_one() -> None:
    spec = {
        'containers': [container('.4m', '.4'), container('.4m', '.4')],
    }

    assert pod_requests(spec) == Resources(2, 2)


def test_pod_requests_apply_classic_init_peak_over_regular_sum() -> None:
    spec = {
        'containers': [container('100m', '100Mi'), container('50m', '50Mi')],
        'initContainers': [container('500m', '20Mi'), container('20m', '600Mi')],
    }

    assert pod_requests(spec) == Resources(500, 600 * 2**20)


def test_pod_requests_follow_restartable_init_sidecar_semantics() -> None:
    spec = {
        'containers': [container('100m', '100Mi')],
        'initContainers': [
            container('30m', '30Mi', restartPolicy='Always'),
            container('200m', '20Mi'),
            container('40m', '40Mi', restartPolicy='Always'),
        ],
    }

    assert pod_requests(spec) == Resources(230, 170 * 2**20)


def test_pod_requests_use_pod_level_max_then_add_overhead() -> None:
    spec = {
        'containers': [container('100m', '100Mi')],
        'initContainers': None,
        'resources': {'requests': {'cpu': '250m', 'memory': '80Mi'}},
        'overhead': {'cpu': '5m', 'memory': '1Mi'},
    }

    assert pod_requests(spec) == Resources(255, 101 * 2**20)


def test_missing_container_requests_are_zero() -> None:
    assert pod_requests({'containers': [{'resources': None}]}) == Resources()
    assert pod_requests({'containers': [{'resources': {'requests': None}}]}) == Resources()


@pytest.mark.parametrize(
    ('spec', 'message'),
    [
        (None, 'pod.spec must be an object'),
        ({}, 'containers must be a list'),
        ({'containers': []}, 'cannot be empty'),
        ({'containers': [None]}, 'container must be an object'),
        (
            {'containers': [{'resources': {'requests': []}}]},
            'resources.requests must be an object',
        ),
        (
            {'containers': [container()], 'initContainers': {}},
            'initContainers must be a list',
        ),
        (
            {
                'containers': [container()],
                'initContainers': [container(restartPolicy='OnFailure')],
            },
            'restartPolicy',
        ),
        (
            {'containers': [container()], 'resources': []},
            'resources must be an object',
        ),
        (
            {'containers': [container()], 'overhead': []},
            'overhead must be an object',
        ),
        (
            {'containers': [container()], 'overhead': {'cpu': 1}},
            'must be a string',
        ),
    ],
)
def test_pod_requests_reject_malformed_specs(spec: object, message: str) -> None:
    with pytest.raises(CapacityError, match=message):
        pod_requests(spec)


def test_snapshot_filters_nodes_and_can_explicitly_include_tainted_nodes() -> None:
    nodes = [
        node('ready'),
        node('not-ready', ready='False'),
        node('unknown', ready='Unknown'),
        node('cordoned', unschedulable=True),
        node('deleting', deleting=True),
        node('tainted', taints=[{'key': 'reserved', 'effect': 'NoSchedule'}]),
        node('prefer', taints=[{'key': 'soft', 'effect': 'PreferNoSchedule'}]),
    ]

    conservative = ClusterSnapshot.build(nodes, [])
    permissive = ClusterSnapshot.build(nodes, [], include_tainted=True)

    assert conservative.ready_nodes == 2
    assert permissive.ready_nodes == 3


def test_snapshot_accepts_null_taints_from_older_api_objects() -> None:
    item = node()
    item['spec']['taints'] = None  # type: ignore[index]
    assert ClusterSnapshot.build([item], []).ready_nodes == 1


def test_snapshot_ignores_well_formed_non_ready_conditions() -> None:
    item = node()
    item['status']['conditions'].insert(  # type: ignore[index]
        0,
        {'type': 'MemoryPressure', 'status': 'False'},
    )
    assert ClusterSnapshot.build([item], []).ready_nodes == 1


def test_snapshot_requires_at_least_one_eligible_node() -> None:
    with pytest.raises(CapacityError, match='no schedulable Ready nodes'):
        ClusterSnapshot.build([node(ready='False')], [])


def test_snapshot_rejects_duplicate_or_contradictory_nodes() -> None:
    with pytest.raises(CapacityError, match='duplicate node'):
        ClusterSnapshot.build([node(), node()], [])

    contradictory = node()
    contradictory['status']['conditions'].append(  # type: ignore[index]
        {'type': 'Ready', 'status': 'False'},
    )
    with pytest.raises(CapacityError, match='contradictory'):
        ClusterSnapshot.build([contradictory], [])


@pytest.mark.parametrize(
    ('mutation', 'message'),
    [
        (lambda _item: None, 'node must be an object'),
        (lambda item: item.update(metadata=None), 'node.metadata must be an object'),
        (
            lambda item: item['metadata'].update(name=''),
            'metadata.name must be a non-empty string',
        ),
        (lambda item: item.update(spec=None), 'node.spec must be an object'),
        (
            lambda item: item['spec'].update(unschedulable='false'),
            'unschedulable must be a boolean',
        ),
        (
            lambda item: item['status'].update(conditions={}),
            'conditions must be a list',
        ),
        (
            lambda item: item['status'].update(conditions=[None]),
            'node condition must be an object',
        ),
        (
            lambda item: item['status'].update(
                conditions=[{'type': 'Ready', 'status': []}],
            ),
            'condition type and status must be strings',
        ),
        (lambda item: item['spec'].update(taints={}), 'taints must be a list'),
        (
            lambda item: item['spec'].update(taints=[None]),
            'node taint must be an object',
        ),
        (
            lambda item: item['spec'].update(taints=[{'effect': []}]),
            'taint effect must be a string',
        ),
        (
            lambda item: item['status'].update(allocatable=None),
            'allocatable must be an object',
        ),
        (
            lambda item: item['status']['allocatable'].update(cpu=1),
            'allocatable cpu, memory, and pods must be strings',
        ),
    ],
)
def test_snapshot_rejects_malformed_ready_nodes(mutation: object, message: str) -> None:
    item = node()
    result = mutation(item)  # type: ignore[operator]
    candidate = result if result is not None else item
    if mutation.__code__.co_consts == (None,):  # type: ignore[attr-defined]
        candidate = None
    with pytest.raises(CapacityError, match=message):
        ClusterSnapshot.build([candidate], [])


def test_snapshot_ignores_terminal_and_unknown_node_pods() -> None:
    pods = [
        pod('succeeded', phase='Succeeded'),
        pod('failed', phase='Failed'),
        pod('elsewhere', node_name='missing'),
    ]
    snapshot = ClusterSnapshot.build([node(cpu='1', memory='1Gi', pods='2')], pods)

    assert snapshot.function_capacity('new', Resources(500, 512 * 2**20)) == 2


def test_snapshot_reserves_unbound_ordinary_pods_after_reclaiming_function_pods() -> None:
    pods = [
        pod('own-a', cpu='500m', memory='512Mi', function_uid='mine'),
        pod('own-b', cpu='500m', memory='512Mi', function_uid='mine'),
        pod('waiting-a', node_name=None, cpu='500m', memory='512Mi', phase='Pending'),
        pod('waiting-b', node_name='', cpu='500m', memory='512Mi', phase='Pending'),
    ]
    snapshot = ClusterSnapshot.build(
        [node(cpu='2', memory='2Gi', pods='4')],
        pods,
    )

    assert snapshot.function_capacity('mine', Resources(500, 512 * 2**20)) == 2


def test_snapshot_does_not_reserve_unbound_function_pods() -> None:
    waiting = pod(
        'waiting-function',
        node_name=None,
        cpu='1',
        memory='1Gi',
        phase='Pending',
        function_uid='other',
    )
    snapshot = ClusterSnapshot.build(
        [node(cpu='2', memory='2Gi', pods='4')],
        [waiting],
    )

    assert snapshot.observed_request('other') == Resources(1_000, 2**30)
    assert snapshot.function_capacity('mine', Resources(500, 512 * 2**20)) == 4


def test_snapshot_uses_nominated_node_for_unbound_pod_reservation() -> None:
    waiting = pod(
        'waiting',
        node_name=None,
        cpu='1',
        memory='1Gi',
        phase='Pending',
    )
    waiting['status']['nominatedNodeName'] = 'node-b'  # type: ignore[index]
    snapshot = ClusterSnapshot.build(
        [
            node('node-a', cpu='2', memory='2Gi', pods='4'),
            node('node-b', cpu='2', memory='2Gi', pods='4'),
        ],
        [waiting],
    )

    assert snapshot.function_capacity('mine', Resources(1_000, 2**30)) == 3


def test_snapshot_ignores_unbound_pod_that_cannot_fit_after_reclaiming_functions() -> None:
    waiting = pod(
        'impossible',
        node_name=None,
        cpu='3',
        memory='1Gi',
        phase='Pending',
    )
    snapshot = ClusterSnapshot.build(
        [node(cpu='2', memory='2Gi', pods='4')],
        [waiting],
    )

    assert snapshot.function_capacity('mine', Resources(500, 512 * 2**20)) == 4


def test_snapshot_subtracts_other_pods_but_reclaims_own_function_pods() -> None:
    pods = [
        pod('normal', cpu='1', memory='1Gi'),
        pod('own-a', cpu='500m', memory='256Mi', function_uid='mine'),
        pod('own-b', cpu='500m', memory='256Mi', function_uid='mine'),
    ]
    snapshot = ClusterSnapshot.build(
        [node(cpu='4', memory='4Gi', pods='5')],
        pods,
    )

    assert snapshot.observed_request('mine') == Resources(500, 256 * 2**20)
    assert snapshot.observed_request('absent') == Resources()
    assert snapshot.function_capacity('mine', Resources(500, 256 * 2**20)) == 4
    assert snapshot.function_capacity('other', Resources(500, 256 * 2**20)) == 2


def test_snapshot_counts_pod_with_null_labels_as_anordinary_workload() -> None:
    item = pod(cpu='500m', memory='256Mi')
    item['metadata']['labels'] = None  # type: ignore[index]
    snapshot = ClusterSnapshot.build([node(cpu='1', memory='1Gi', pods='2')], [item])
    assert snapshot.function_capacity('fn', Resources(500, 256 * 2**20)) == 1


def test_snapshot_capacity_is_per_node_and_respects_every_dimension() -> None:
    snapshot = ClusterSnapshot.build(
        [
            node('cpu-rich', cpu='2', memory='512Mi', pods='10'),
            node('memory-rich', cpu='500m', memory='2Gi', pods='1'),
        ],
        [],
    )

    assert snapshot.function_capacity('fn', Resources(1_000, 2**30)) == 0
    assert snapshot.function_capacity('fn', Resources(250, 256 * 2**20)) == 3


def test_snapshot_clamps_overcommitted_node_resources_and_slots_to_zero() -> None:
    snapshot = ClusterSnapshot.build(
        [node(cpu='1', memory='1Gi', pods='1')],
        [pod(cpu='2', memory='2Gi')],
    )

    assert snapshot.function_capacity('fn', Resources(1, 1)) == 0


@pytest.mark.parametrize(
    ('uid', 'resources', 'message'),
    [
        ('', Resources(1, 1), 'UID'),
        ('uid', Resources(0, 1), 'positive'),
        ('uid', Resources(1, 0), 'positive'),
    ],
)
def test_snapshot_rejects_invalid_function_inputs(
    uid: str,
    resources: Resources,
    message: str,
) -> None:
    snapshot = ClusterSnapshot.build([node()], [])
    with pytest.raises(CapacityError, match=message):
        snapshot.function_capacity(uid, resources)


@pytest.mark.parametrize(
    ('mutation', 'message'),
    [
        (lambda _item: None, 'pod must be an object'),
        (lambda item: item.update(spec=None), 'pod.spec must be an object'),
        (
            lambda item: item['status'].update(phase=1),
            'pod.status.phase must be a string',
        ),
        (
            lambda item: item['spec'].update(nodeName=1),
            'pod.spec.nodeName must be a string',
        ),
        (
            lambda item: item['status'].update(nominatedNodeName=1),
            'pod.status.nominatedNodeName must be a string',
        ),
        (
            lambda item: item['metadata'].update(namespace=''),
            'pod.metadata.namespace must be a non-empty string',
        ),
        (
            lambda item: item['metadata'].update(labels=[]),
            'pod.metadata.labels must be an object',
        ),
        (
            lambda item: item['metadata']['labels'].update(functionUid=1),
            'functionUid label must be a string',
        ),
    ],
)
def test_snapshot_rejects_malformed_active_pods(mutation: object, message: str) -> None:
    item = pod()
    result = mutation(item)  # type: ignore[operator]
    candidate = result if result is not None else item
    if mutation.__code__.co_consts == (None,):  # type: ignore[attr-defined]
        candidate = None
    with pytest.raises(CapacityError, match=message):
        ClusterSnapshot.build([node()], [candidate])


def test_index_environments_uses_namespace_and_name_without_mutation() -> None:
    first = environment(namespace='one')
    second = environment(namespace='two')
    original = deepcopy([first, second])

    result = index_environments([first, second])

    assert result == {('one', 'python'): first, ('two', 'python'): second}
    assert [first, second] == original


@pytest.mark.parametrize(
    ('items', 'message'),
    [
        ([None], 'environment must be an object'),
        ([{'metadata': None}], 'environment.metadata must be an object'),
        ([environment(namespace='')], 'namespace must be a string'),
        ([environment(name='')], 'metadata.name must be a non-empty string'),
        ([environment(), environment()], 'duplicate environment'),
    ],
)
def test_index_environments_rejects_malformed_or_duplicate_objects(
    items: list[object],
    message: str,
) -> None:
    with pytest.raises(CapacityError, match=message):
        index_environments(items)


def test_function_request_inherits_and_partially_overrides_environment() -> None:
    environments = index_environments([environment(cpu='500m', memory='256Mi')])

    inherited = function(cpu=None, memory=None)
    cpu_override = function(cpu='1', memory=None)
    memory_override = function(cpu=None, memory='512Mi')
    zero_override = function(cpu='0', memory='0')

    assert function_request(inherited, environments, Resources(10, 16 * 2**20)) == Resources(
        510,
        272 * 2**20,
    )
    assert function_request(cpu_override, environments, Resources()) == Resources(
        1_000,
        256 * 2**20,
    )
    assert function_request(memory_override, environments, Resources()) == Resources(
        500,
        512 * 2**20,
    )
    assert function_request(zero_override, environments, Resources()) == Resources(
        500,
        256 * 2**20,
    )


def test_function_request_defaults_empty_environment_namespace_to_function_namespace() -> None:
    item = function(environment_namespace='')
    result = function_request(
        item,
        index_environments([environment()]),
        Resources(),
    )
    assert result == Resources(250, 128 * 2**20)


def test_function_request_applies_pod_defaulting_from_limits_when_requests_are_absent() -> None:
    environments = index_environments(
        [
            environment(
                cpu=None,
                memory=None,
                cpu_limit='500m',
                memory_limit='256Mi',
            ),
        ],
    )

    inherited_limits = function(cpu=None, memory=None)
    overridden_limits = function(
        cpu=None,
        memory=None,
        cpu_limit='1',
        memory_limit='512Mi',
    )

    assert function_request(inherited_limits, environments, Resources()) == Resources(
        500,
        256 * 2**20,
    )
    assert function_request(overridden_limits, environments, Resources()) == Resources(
        1_000,
        512 * 2**20,
    )


def test_explicit_zero_environment_request_is_not_defaulted_from_limit() -> None:
    environments = index_environments(
        [environment(cpu='0', memory='0', cpu_limit='1', memory_limit='1Gi')],
    )
    with pytest.raises(CapacityError, match='container requests must be positive'):
        function_request(function(cpu=None, memory=None), environments, Resources(10, 10))


@pytest.mark.parametrize(
    ('mutate', 'message'),
    [
        (lambda _item: None, 'function must be an object'),
        (lambda item: item.update(metadata=None), 'function.metadata must be an object'),
        (
            lambda item: item['metadata'].update(namespace=''),
            'function.metadata.namespace must be a string',
        ),
        (lambda item: item.update(spec=None), 'function.spec must be an object'),
        (
            lambda item: item['spec'].update(environment=None),
            'function.spec.environment must be an object',
        ),
        (
            lambda item: item['spec']['environment'].update(name=''),
            'environment name must be a string',
        ),
        (
            lambda item: item['spec']['environment'].update(namespace=1),
            'environment namespace must be a string',
        ),
        (
            lambda item: item['spec'].update(resources=None),
            'function.spec.resources must be an object',
        ),
        (
            lambda item: item['spec']['resources'].update(requests=None),
            'resolved function container requests must be positive',
        ),
        (
            lambda item: item['spec']['resources']['requests'].update(cpu=1),
            'requests cpu quantity must be a string',
        ),
        (
            lambda item: item['spec']['resources']['requests'].update(memory=1),
            'requests memory quantity must be a string',
        ),
    ],
)
def test_function_request_rejects_malformed_objects(mutate: object, message: str) -> None:
    item = function()
    result = mutate(item)  # type: ignore[operator]
    candidate = result if result is not None else item
    if mutate.__code__.co_consts == (None,):  # type: ignore[attr-defined]
        candidate = None
    environments = index_environments([environment(cpu=None, memory=None)])
    with pytest.raises(CapacityError, match=message):
        function_request(candidate, environments, Resources())


def test_function_request_reports_missing_environment() -> None:
    with pytest.raises(CapacityError, match='was not found'):
        function_request(function(), {}, Resources())
