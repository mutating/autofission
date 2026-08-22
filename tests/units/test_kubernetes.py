from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import urllib3
from kubernetes.client.exceptions import ApiException

from autofission.errors import ConfigurationError, KubernetesProtocolError
from autofission.kubernetes import KubernetesClient


class FakeApiClient:
    def __init__(self, configuration: object = None) -> None:
        self.configuration = configuration
        self.closed = False

    @staticmethod
    def sanitize_for_serialization(value: object) -> object:
        if hasattr(value, 'document'):
            return value.document
        return value

    def close(self) -> None:
        self.closed = True


def _client() -> KubernetesClient:
    result = KubernetesClient(FakeApiClient())
    result._core = SimpleNamespace()
    result._custom = SimpleNamespace()
    return result


def test_paginate_preserves_order_kwargs_and_opaque_tokens() -> None:
    calls: list[dict[str, object]] = []
    pages = [
        {'items': [1], 'metadata': {'continue': 'abc+/= ?&%'}},
        {'items': [2], 'metadata': {'continue': 'next'}},
        {'items': [3]},
    ]

    def list_call(**kwargs: object) -> object:
        calls.append(kwargs)
        return pages.pop(0)

    result = _client()._paginate(list_call, label_selector='x=y')

    assert result == [1, 2, 3]
    assert [call['_continue'] for call in calls] == [None, 'abc+/= ?&%', 'next']
    assert all(call['limit'] == 500 for call in calls)
    assert all(call['label_selector'] == 'x=y' for call in calls)
    assert all(call['_request_timeout'] == 10 for call in calls)


@pytest.mark.parametrize(
    'tokens',
    [
        ['same', 'same'],
        ['a', 'b', 'a'],
    ],
)
def test_paginate_detects_repeated_or_cyclic_tokens(tokens: list[str]) -> None:
    pages = [{'items': [], 'metadata': {'continue': token}} for token in tokens]
    with pytest.raises(KubernetesProtocolError, match='cycle'):
        _client()._paginate(lambda **_kwargs: pages.pop(0))


def test_paginate_rejects_an_unbounded_sequence_of_unique_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('autofission.kubernetes.MAX_LIST_PAGES', 2)
    counter = 0

    def list_call(**_kwargs: object) -> object:
        nonlocal counter
        counter += 1
        return {'items': [], 'metadata': {'continue': f'token-{counter}'}}

    with pytest.raises(KubernetesProtocolError, match='exceeded 2 pages'):
        _client()._paginate(list_call)


def test_paginate_restarts_once_after_resource_expired_without_mixing_items() -> None:
    calls = 0

    def list_call(**_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {'items': ['stale'], 'metadata': {'continue': 'old'}}
        if calls == 2:
            raise ApiException(status=410, reason='Expired')
        return {'items': ['fresh'], 'metadata': {}}

    assert _client()._paginate(list_call) == ['fresh']
    assert calls == 3


def test_paginate_propagates_a_second_resource_expired_error() -> None:
    def list_call(**_kwargs: object) -> object:
        raise ApiException(status=410, reason='Expired')

    client = _client()
    with pytest.raises(ApiException):
        client._paginate(list_call)


def test_paginate_propagates_non_resource_expired_api_errors() -> None:
    def list_call(**_kwargs: object) -> object:
        raise ApiException(status=403, reason='Forbidden')

    with pytest.raises(ApiException):
        _client()._paginate(list_call)


@pytest.mark.parametrize(
    ('document', 'message'),
    [
        ([], 'response must be an object'),
        ({}, 'response.items must be a list'),
        ({'items': None}, 'response.items must be a list'),
        ({'items': [], 'metadata': None}, 'response.metadata must be an object'),
        (
            {'items': [], 'metadata': {'continue': 1}},
            'continue token must be a string',
        ),
    ],
)
def test_paginate_rejects_malformed_api_documents(
    document: object,
    message: str,
) -> None:
    with pytest.raises(KubernetesProtocolError, match=message):
        _client()._paginate(lambda **_kwargs: document)


def test_paginate_sanitizes_official_client_models() -> None:
    model = SimpleNamespace(document={'items': ['model'], 'metadata': {}})
    assert _client()._paginate(lambda **_kwargs: model) == ['model']


def test_public_list_methods_call_the_correct_apis() -> None:
    client = _client()
    core_calls: list[tuple[str, dict[str, object]]] = []
    custom_calls: list[dict[str, object]] = []

    def core_call(kind: str) -> object:
        def call(**kwargs: object) -> object:
            core_calls.append((kind, kwargs))
            return {'items': [kind], 'metadata': {}}

        return call

    def custom_call(**kwargs: object) -> object:
        custom_calls.append(kwargs)
        return {'items': [kwargs['plural']], 'metadata': {}}

    client._core = SimpleNamespace(
        list_node=core_call('nodes'),
        list_pod_for_all_namespaces=core_call('pods'),
    )
    client._custom = SimpleNamespace(
        list_cluster_custom_object=custom_call,
    )

    assert client.list_nodes() == ['nodes']
    assert client.list_pods() == ['pods']
    assert client.list_functions('managed=true') == ['functions']
    assert client.list_environments() == ['environments']
    assert [kind for kind, _ in core_calls] == ['nodes', 'pods']
    assert custom_calls[0]['group'] == 'fission.io'
    assert custom_calls[0]['version'] == 'v1'
    assert custom_calls[0]['plural'] == 'functions'
    assert custom_calls[0]['label_selector'] == 'managed=true'
    assert custom_calls[1]['plural'] == 'environments'


def test_patch_function_sends_merge_object_and_supports_empty_response() -> None:
    calls: list[dict[str, object]] = []
    client = _client()
    client._custom = SimpleNamespace(
        patch_namespaced_custom_object=lambda **kwargs: calls.append(kwargs),
    )
    body = {'metadata': {'resourceVersion': '1'}}

    assert client.patch_function('ns / ?', 'name/%', body) is None
    assert calls == [
        {
            'group': 'fission.io',
            'version': 'v1',
            'namespace': 'ns / ?',
            'plural': 'functions',
            'name': 'name/%',
            'body': body,
            '_request_timeout': 10,
        },
    ]


@pytest.mark.parametrize(('namespace', 'name'), [('', 'name'), ('ns', '')])
def test_patch_function_rejects_empty_identity(namespace: str, name: str) -> None:
    with pytest.raises(ValueError, match='cannot be empty'):
        _client().patch_function(namespace, name, {})


@pytest.mark.parametrize('timeout', [0.0, -1.0, float('nan'), float('inf')])
def test_client_rejects_non_positive_or_non_finite_timeout(timeout: float) -> None:
    with pytest.raises(ConfigurationError, match='finite and positive'):
        KubernetesClient(FakeApiClient(), timeout)


def test_close_closes_official_api_client() -> None:
    api_client = FakeApiClient()
    client = KubernetesClient(api_client)
    client.close()
    assert api_client.closed


def test_create_auto_selects_in_cluster_and_configures_get_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[tuple[str, object]] = []
    monkeypatch.setenv('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
    monkeypatch.setattr(
        'autofission.kubernetes.config.load_incluster_config',
        lambda *, client_configuration: loaded.append(('cluster', client_configuration)),
    )
    monkeypatch.setattr(
        'autofission.kubernetes.client.ApiClient',
        FakeApiClient,
    )

    gateway = KubernetesClient.create(request_timeout=7, retry_attempts=2)
    configuration = gateway._api_client.configuration

    assert loaded == [('cluster', configuration)]
    assert isinstance(configuration.retries, urllib3.Retry)
    assert configuration.retries.total == 2
    assert configuration.retries.allowed_methods == frozenset({'GET'})
    assert configuration.retries.status_forcelist == (429, 500, 502, 503, 504)
    assert configuration.retries.respect_retry_after_header


def test_create_loads_explicit_kubeconfig_and_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loaded: list[dict[str, object]] = []
    kubeconfig = tmp_path / 'config'
    monkeypatch.setenv('KUBERNETES_SERVICE_HOST', 'ignored')
    monkeypatch.setattr(
        'autofission.kubernetes.config.load_kube_config',
        lambda **kwargs: loaded.append(kwargs),
    )
    monkeypatch.setattr('autofission.kubernetes.client.ApiClient', FakeApiClient)

    KubernetesClient.create(kubeconfig=kubeconfig, context='test')

    assert loaded[0]['config_file'] == str(kubeconfig)
    assert loaded[0]['context'] == 'test'


def test_create_uses_default_kubeconfig_outside_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[dict[str, object]] = []
    monkeypatch.delenv('KUBERNETES_SERVICE_HOST', raising=False)
    monkeypatch.setattr(
        'autofission.kubernetes.config.load_kube_config',
        lambda **kwargs: loaded.append(kwargs),
    )
    monkeypatch.setattr('autofission.kubernetes.client.ApiClient', FakeApiClient)

    KubernetesClient.create()

    assert loaded[0]['config_file'] is None
    assert loaded[0]['context'] is None


@pytest.mark.parametrize(
    'kwargs',
    [
        {'retry_attempts': -1},
        {'retry_attempts': True},
        {'kubeconfig': Path('/tmp/config'), 'in_cluster': True},
        {'context': 'test', 'in_cluster': True},
    ],
)
def test_create_rejects_invalid_or_conflicting_configuration(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError):
        KubernetesClient.create(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize('in_cluster', [True, False])
def test_create_wraps_credential_loading_errors(
    monkeypatch: pytest.MonkeyPatch,
    in_cluster: bool,
) -> None:
    def fail(**_kwargs: object) -> None:
        raise RuntimeError('credentials unavailable')

    monkeypatch.setattr(
        'autofission.kubernetes.config.load_incluster_config',
        fail,
    )
    monkeypatch.setattr('autofission.kubernetes.config.load_kube_config', fail)

    with pytest.raises(ConfigurationError, match='could not load'):
        KubernetesClient.create(in_cluster=in_cluster)
