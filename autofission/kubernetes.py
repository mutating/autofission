"""Official Kubernetes-client adapter with bounded, guarded pagination."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from math import isfinite
from pathlib import Path
from typing import Any

import urllib3
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from autofission.errors import ConfigurationError, KubernetesProtocolError

FISSION_GROUP = 'fission.io'
FISSION_VERSION = 'v1'
FUNCTION_PLURAL = 'functions'
ENVIRONMENT_PLURAL = 'environments'
MAX_LIST_PAGES = 10_000


class KubernetesClient:
    """Translate official client models into plain JSON-compatible objects."""

    def __init__(self, api_client: client.ApiClient, request_timeout: float = 10.0) -> None:
        if not isfinite(request_timeout) or request_timeout <= 0:
            raise ConfigurationError('request timeout must be finite and positive')
        self._api_client = api_client
        self._core = client.CoreV1Api(api_client)
        self._custom = client.CustomObjectsApi(api_client)
        self._request_timeout = request_timeout

    @classmethod
    def create(
        cls,
        *,
        kubeconfig: Path | None = None,
        context: str | None = None,
        in_cluster: bool | None = None,
        request_timeout: float = 10.0,
        retry_attempts: int = 3,
    ) -> KubernetesClient:
        """Load in-cluster credentials or a kubeconfig and construct a client."""
        if isinstance(retry_attempts, bool) or retry_attempts < 0:
            raise ConfigurationError('retry attempts must be a non-negative integer')
        if kubeconfig is not None and in_cluster is True:
            raise ConfigurationError('--kubeconfig and --in-cluster are mutually exclusive')
        if context is not None and in_cluster is True:
            raise ConfigurationError('--context and --in-cluster are mutually exclusive')

        configuration = client.Configuration()
        auto_in_cluster = bool(os.environ.get('KUBERNETES_SERVICE_HOST'))
        use_in_cluster = in_cluster is True or (
            in_cluster is None and kubeconfig is None and context is None and auto_in_cluster
        )
        try:
            if use_in_cluster:
                config.load_incluster_config(client_configuration=configuration)
            else:
                config.load_kube_config(
                    config_file=None if kubeconfig is None else str(kubeconfig),
                    context=context,
                    client_configuration=configuration,
                )
        except Exception as error:
            source = 'in-cluster credentials' if use_in_cluster else 'kubeconfig'
            raise ConfigurationError(f'could not load {source}: {error}') from error

        configuration.retries = urllib3.Retry(
            total=retry_attempts,
            connect=retry_attempts,
            read=retry_attempts,
            status=retry_attempts,
            allowed_methods=frozenset({'GET'}),
            status_forcelist=(429, 500, 502, 503, 504),
            backoff_factor=0.5,
            respect_retry_after_header=True,
        )
        return cls(client.ApiClient(configuration), request_timeout)

    def close(self) -> None:
        """Release the official client's connection pool."""
        self._api_client.close()

    def list_nodes(self) -> list[object]:
        return self._paginate(self._core.list_node)

    def list_pods(self) -> list[object]:
        return self._paginate(self._core.list_pod_for_all_namespaces)

    def list_functions(self, label_selector: str) -> list[object]:
        return self._paginate(
            self._custom.list_cluster_custom_object,
            group=FISSION_GROUP,
            version=FISSION_VERSION,
            plural=FUNCTION_PLURAL,
            label_selector=label_selector,
        )

    def list_environments(self) -> list[object]:
        return self._paginate(
            self._custom.list_cluster_custom_object,
            group=FISSION_GROUP,
            version=FISSION_VERSION,
            plural=ENVIRONMENT_PLURAL,
        )

    def patch_function(
        self,
        namespace: str,
        name: str,
        body: Mapping[str, object],
    ) -> object:
        if not namespace or not name:
            raise ValueError('function namespace and name cannot be empty')
        return self._custom.patch_namespaced_custom_object(
            group=FISSION_GROUP,
            version=FISSION_VERSION,
            namespace=namespace,
            plural=FUNCTION_PLURAL,
            name=name,
            body=dict(body),
            _request_timeout=self._request_timeout,
        )

    def _paginate(
        self,
        list_call: Callable[..., object],
        **kwargs: object,
    ) -> list[object]:
        items: list[object] = []
        continuation = ''
        seen: set[str] = set()
        restarted = False
        page_count = 0
        while True:
            page_count += 1
            if page_count > MAX_LIST_PAGES:
                raise KubernetesProtocolError(
                    f'Kubernetes list exceeded {MAX_LIST_PAGES} pages',
                )
            try:
                response = list_call(
                    limit=500,
                    _continue=continuation or None,
                    _request_timeout=self._request_timeout,
                    **kwargs,
                )
            except ApiException as error:
                if error.status == 410 and not restarted:
                    items.clear()
                    continuation = ''
                    seen.clear()
                    restarted = True
                    page_count = 0
                    continue
                raise

            document = self._api_client.sanitize_for_serialization(response)
            if not isinstance(document, Mapping):
                raise KubernetesProtocolError('Kubernetes list response must be an object')
            page = document.get('items')
            if not isinstance(page, list):
                raise KubernetesProtocolError('Kubernetes list response.items must be a list')
            items.extend(page)

            metadata_value = document.get('metadata', {})
            if not isinstance(metadata_value, Mapping):
                raise KubernetesProtocolError(
                    'Kubernetes list response.metadata must be an object',
                )
            token: Any = metadata_value.get('continue', '')
            if not isinstance(token, str):
                raise KubernetesProtocolError('Kubernetes continue token must be a string')
            if not token:
                return items
            if token in seen:
                raise KubernetesProtocolError('Kubernetes pagination token cycle detected')
            seen.add(token)
            continuation = token
