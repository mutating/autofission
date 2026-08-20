from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy

import pytest

from autofission.controller import (
    ANNOTATION_PREFIX,
    INT32_MAX,
    Controller,
    ControllerConfig,
)
from autofission.errors import ReconcileError
from autofission.models import Resources

from ..helpers import environment, function, node, pod


class FakeGateway:
    def __init__(
        self,
        *,
        nodes: list[object] | None = None,
        pods: list[object] | None = None,
        functions: list[object] | None = None,
        environments: list[object] | None = None,
        patch_errors: Mapping[str, Exception] | None = None,
    ) -> None:
        self.nodes: list[object] = [node()] if nodes is None else nodes
        self.pods: list[object] = [] if pods is None else pods
        self.functions: list[object] = [function()] if functions is None else functions
        self.environments: list[object] = [environment()] if environments is None else environments
        self.patch_errors = {} if patch_errors is None else dict(patch_errors)
        self.selectors: list[str] = []
        self.patches: list[tuple[str, str, Mapping[str, object]]] = []

    def list_nodes(self) -> list[object]:
        return self.nodes

    def list_pods(self) -> list[object]:
        return self.pods

    def list_functions(self, label_selector: str) -> list[object]:
        self.selectors.append(label_selector)
        return self.functions

    def list_environments(self) -> list[object]:
        return self.environments

    def patch_function(
        self,
        namespace: str,
        name: str,
        body: Mapping[str, object],
    ) -> object:
        self.patches.append((namespace, name, body))
        if name in self.patch_errors:
            raise self.patch_errors[name]
        return {}


def _annotations(maximum: int, ready_nodes: int = 1) -> dict[str, str]:
    return {
        f'{ANNOTATION_PREFIX}/calculated-maxscale': str(maximum),
        f'{ANNOTATION_PREFIX}/ready-nodes': str(ready_nodes),
    }


def test_reconcile_updates_maxscale_with_resource_version_precondition() -> None:
    gateway = FakeGateway()
    controller = Controller(gateway)

    result = controller.reconcile()

    assert result.managed_functions == 1
    assert result.updated_functions == 1
    assert result.ready_nodes == 1
    assert gateway.selectors == ['autoscaling.fission.io/cluster-capacity=true']
    assert gateway.patches == [
        (
            'fission-function',
            'hello',
            {
                'metadata': {
                    'resourceVersion': '7',
                    'annotations': _annotations(15),
                },
                'spec': {
                    'InvokeStrategy': {
                        'ExecutionStrategy': {'MaxScale': 15},
                    },
                },
            },
        ),
    ]


def test_reconcile_is_idempotent_and_preserves_unrelated_annotations() -> None:
    item = function(
        maximum=15,
        annotations={**_annotations(15), 'example.com/owner': 'user'},
    )
    gateway = FakeGateway(functions=[item])

    result = Controller(gateway).reconcile()

    assert result.updated_functions == 0
    assert gateway.patches == []
    assert item['metadata']['annotations']['example.com/owner'] == 'user'  # type: ignore[index]


@pytest.mark.parametrize(
    'item',
    [
        function(maximum=14, annotations=_annotations(15)),
        function(maximum=15, annotations=_annotations(14)),
        function(maximum=15, annotations=None),
    ],
)
def test_reconcile_patches_if_maximum_or_managed_annotation_drifts(
    item: dict[str, object],
) -> None:
    gateway = FakeGateway(functions=[item])
    assert Controller(gateway).reconcile().updated_functions == 1
    assert len(gateway.patches) == 1


def test_reconcile_uses_minimum_one_and_respects_min_scale() -> None:
    tiny_node = node(cpu='1m', memory='1', pods='0')
    first = function(name='one', uid='one', minimum=0, maximum=3)
    second = function(name='minimum', uid='two', minimum=7, maximum=7)
    gateway = FakeGateway(nodes=[tiny_node], functions=[first, second])

    Controller(gateway).reconcile()

    maxima = [
        patch[2]['spec']['InvokeStrategy']['ExecutionStrategy']['MaxScale']  # type: ignore[index]
        for patch in gateway.patches
    ]
    assert maxima == [1, 7]


def test_reconcile_observes_larger_real_function_pod_request() -> None:
    existing = pod(cpu='1', memory='1Gi', function_uid='uid-1')
    gateway = FakeGateway(pods=[existing])

    Controller(gateway).reconcile()

    maximum = gateway.patches[0][2]['spec']['InvokeStrategy']['ExecutionStrategy'][  # type: ignore[index]
        'MaxScale'
    ]
    assert maximum == 4


def test_reconcile_skips_unmanaged_and_terminating_functions() -> None:
    unmanaged = function(name='plain', managed=False)
    no_labels = function(name='none')
    no_labels['metadata']['labels'] = None  # type: ignore[index]
    terminating = function(name='gone')
    terminating['metadata']['deletionTimestamp'] = 'now'  # type: ignore[index]
    gateway = FakeGateway(functions=[unmanaged, no_labels, terminating])

    result = Controller(gateway).reconcile()

    assert result.managed_functions == 1
    assert result.updated_functions == 0
    assert gateway.patches == []


def test_reconcile_custom_label_and_tainted_node_policy() -> None:
    item = function(managed=False)
    item['metadata']['labels'] = {'example.com/autofission': 'yes'}  # type: ignore[index]
    gateway = FakeGateway(
        nodes=[node(taints=[{'effect': 'NoSchedule'}])],
        functions=[item],
    )
    config = ControllerConfig(
        managed_label='example.com/autofission',
        managed_value='yes',
        include_tainted_nodes=True,
    )

    result = Controller(gateway, config).reconcile()

    assert result.managed_functions == 1
    assert gateway.selectors == ['example.com/autofission=yes']


def test_each_function_failure_is_isolated_and_cycle_is_degraded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid = function(name='pool', executor='poolmgr')
    patch_failure = function(name='conflict', uid='uid-2')
    valid = function(name='valid', uid='uid-3')
    gateway = FakeGateway(
        functions=[invalid, patch_failure, valid],
        patch_errors={'conflict': RuntimeError('409 Conflict')},
    )

    with caplog.at_level(logging.ERROR), pytest.raises(ReconcileError) as captured:
        Controller(gateway).reconcile()

    assert captured.value.failures == ('fission-function/pool', 'fission-function/conflict')
    assert [patch[1] for patch in gateway.patches] == ['conflict', 'valid']
    assert 'unsupported executor' in caplog.text
    assert 'RuntimeError' in caplog.text


def test_malformed_function_without_metadata_does_not_hide_later_valid_function() -> None:
    gateway = FakeGateway(functions=[None, function(name='valid')])

    with pytest.raises(ReconcileError) as captured:
        Controller(gateway).reconcile()

    assert captured.value.failures == ('?/?',)
    assert gateway.patches[0][1] == 'valid'


@pytest.mark.parametrize(
    ('mutate', 'message'),
    [
        (
            lambda item: item['metadata'].update(labels=[]),
            'function.metadata.labels must be an object',
        ),
        (
            lambda item: item['metadata'].update(namespace=''),
            'function namespace must be a non-empty string',
        ),
        (
            lambda item: item['metadata'].update(name=''),
            'function name must be a non-empty string',
        ),
        (
            lambda item: item['metadata'].update(uid=''),
            'function UID must be a non-empty string',
        ),
        (
            lambda item: item['metadata'].update(resourceVersion=''),
            'resourceVersion must be a non-empty string',
        ),
        (lambda item: item.update(spec=None), 'function.spec must be an object'),
        (
            lambda item: item['spec'].update(InvokeStrategy=None),
            'InvokeStrategy must be an object',
        ),
        (
            lambda item: item['spec']['InvokeStrategy'].update(ExecutionStrategy=None),
            'ExecutionStrategy must be an object',
        ),
        (
            lambda item: item['spec']['InvokeStrategy']['ExecutionStrategy'].update(
                MinScale=True,
            ),
            'MinScale must be an integer',
        ),
        (
            lambda item: item['spec']['InvokeStrategy']['ExecutionStrategy'].update(
                MinScale=-1,
            ),
            'MinScale must be between',
        ),
        (
            lambda item: item['spec']['InvokeStrategy']['ExecutionStrategy'].update(
                MinScale=INT32_MAX + 1,
            ),
            'MinScale must be between',
        ),
        (
            lambda item: item['spec']['InvokeStrategy']['ExecutionStrategy'].update(
                MaxScale=None,
            ),
            'MaxScale must be an integer',
        ),
        (
            lambda item: item['spec']['InvokeStrategy']['ExecutionStrategy'].update(
                MaxScale=0,
            ),
            'MaxScale must be positive',
        ),
        (
            lambda item: item['metadata'].update(annotations=[]),
            'annotations must be an object',
        ),
    ],
)
def test_malformed_managed_function_is_reported(
    mutate: object,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = function()
    mutate(item)  # type: ignore[operator]
    gateway = FakeGateway(functions=[item])

    with caplog.at_level(logging.ERROR), pytest.raises(ReconcileError):
        Controller(gateway).reconcile()
    assert message in caplog.text


def test_min_scale_defaults_when_field_is_absent() -> None:
    item = function()
    del item['spec']['InvokeStrategy']['ExecutionStrategy']['MinScale']  # type: ignore[index]
    gateway = FakeGateway(functions=[item])

    assert Controller(gateway).reconcile().updated_functions == 1


def test_calculated_maximum_is_clamped_to_hpa_int32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        'autofission.capacity.ClusterSnapshot.function_capacity',
        lambda self, uid, request: INT32_MAX + 1,
    )
    gateway = FakeGateway()

    Controller(gateway).reconcile()

    maximum = gateway.patches[0][2]['spec']['InvokeStrategy']['ExecutionStrategy'][  # type: ignore[index]
        'MaxScale'
    ]
    assert maximum == INT32_MAX


@pytest.mark.parametrize(
    'config',
    [
        ControllerConfig(fetcher_request=Resources()),
        ControllerConfig(),
    ],
)
def test_controller_does_not_mutate_input_objects(config: ControllerConfig) -> None:
    functions: list[object] = [function()]
    environments: list[object] = [environment()]
    nodes: list[object] = [node()]
    originals = deepcopy((functions, environments, nodes))

    Controller(
        FakeGateway(functions=functions, environments=environments, nodes=nodes),
        config,
    ).reconcile()

    assert (functions, environments, nodes) == originals


@pytest.mark.parametrize(
    ('kwargs', 'message'),
    [
        ({'fetcher_request': Resources(-1, 0)}, 'CPU'),
        ({'fetcher_request': Resources(0, -1)}, 'memory'),
        ({'managed_label': ''}, 'label'),
        ({'managed_value': ''}, 'value'),
    ],
)
def test_controller_config_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ControllerConfig(**kwargs)  # type: ignore[arg-type]
