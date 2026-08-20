<details>
  <summary>ⓘ</summary>

[![Tests](https://github.com/pomponchik/autofission/actions/workflows/tests_and_coverage.yml/badge.svg?branch=develop)](https://github.com/pomponchik/autofission/actions/workflows/tests_and_coverage.yml)
[![Lint](https://github.com/pomponchik/autofission/actions/workflows/lint.yml/badge.svg?branch=develop)](https://github.com/pomponchik/autofission/actions/workflows/lint.yml)
[![Python versions](https://img.shields.io/pypi/pyversions/autofission.svg)](https://pypi.org/project/autofission/)
[![PyPI version](https://badge.fury.io/py/autofission.svg)](https://pypi.org/project/autofission/)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

</details>

![Autofission](https://raw.githubusercontent.com/pomponchik/autofission/develop/docs/assets/logo.svg)

Autofission dynamically calculates and updates the maximum replica limit (`MaxScale`) for explicitly opted-in [Fission](https://fission.io/) Functions based on the Kubernetes cluster's current schedulable capacity.

A Fission Function using the `newdeploy` executor can scale down after demand disappears, but its Horizontal Pod Autoscaler (HPA) still needs a fixed positive `MaxScale`. A `MaxScale` value sized for today's cluster becomes an artificial ceiling after nodes are added, while an arbitrarily huge value can flood the scheduler with Pods that cannot fit.

To keep that ceiling useful, Autofission continuously measures schedulable capacity node by node, subtracts the requests of existing workloads, and accounts for the function's own replicas. It is intended for elastic, bare-metal, homelab, and edge clusters where nodes come and go and idle compute should be available to functions without displacing ordinary services.

Autofission only sets this upper bound; Fission's HPA and idle reaper still decide when the replica count grows and shrinks.


## Table of Contents

- [**Installation**](#installation)
- [**Quick start**](#quick-start)
- [**How it works**](#how-it-works)
- [**Configuration**](#configuration)
- [**RBAC and security**](#rbac-and-security)
- [**Operations**](#operations)
- [**Compatibility and limitations**](#compatibility-and-limitations)
- [**Troubleshooting**](#troubleshooting)
- [**Development and releases**](#development-and-releases)


## Installation

Use the Python CLI for optional local or one-shot runs. Install the Helm chart to run Autofission continuously in a cluster.

### Python CLI

For local use, Autofission requires Python 3.8 or newer and is tested through Python 3.15, including free-threaded Python 3.14:

```bash
python -m pip install autofission
autofission --help
```

Outside a Pod, the CLI uses the current kubeconfig context. Inside Kubernetes it automatically uses the mounted ServiceAccount credentials. `python -m autofission` and the `autofission` entry point are equivalent.

### Install in a cluster

Prerequisites are Kubernetes and an existing Fission installation. Autofission manages only Functions that use the `newdeploy` executor. The chart creates Autofission's ServiceAccount, least-privilege RBAC, controller Deployment, health probes, and two PriorityClasses; it does not install or remove Fission.

Set `AUTOFISSION_VERSION` to the chart version you want to install, then install Autofission from its OCI release:

```bash
helm upgrade --install autofission \
  oci://ghcr.io/pomponchik/charts/autofission \
  --version "${AUTOFISSION_VERSION}" \
  --namespace fission \
  --create-namespace \
  --atomic \
  --wait
```

For an unreleased checkout, replace the OCI URL with `./deploy/helm/autofission`. If the fetcher requests configured for Fission differ from Autofission's defaults (`10m` CPU and `16Mi` memory), set matching chart values so the capacity calculation remains accurate:

```bash
helm upgrade --install autofission ./deploy/helm/autofission \
  --namespace fission \
  --set controller.fetcherCpuRequest=20m \
  --set controller.fetcherMemoryRequest=32Mi
```

After the chart creates its PriorityClasses, make elastic Fission runtime Pods lower priority and non-preempting so ordinary workloads take precedence. For an Autofission Helm release named `autofission`, add this to your Fission Helm values, then install or upgrade Fission with the updated values before opting in any Functions:

```yaml
runtimePodSpec:
  enabled: true
  podSpec:
    priorityClassName: autofission-runtime
```

## Quick start

The commands below use a Function named `hello` in the `fission-function` namespace; replace both with your Function's name and namespace. Opt that `newdeploy` Function in:

```bash
kubectl label function hello \
  --namespace fission-function \
  autoscaling.fission.io/cluster-capacity=true
```

Wait for one reconciliation interval (15 seconds by default), then inspect the Function's `MaxScale`, the recorded calculation, the resulting HPA, and the controller logs:

```bash
kubectl get function hello --namespace fission-function \
  -o jsonpath='{.spec.InvokeStrategy.ExecutionStrategy.MaxScale}{"\n"}'
kubectl get function hello --namespace fission-function \
  -o jsonpath='{.metadata.annotations.autoscaling\.fission\.io/calculated-maxscale}{"\n"}'
kubectl get hpa --namespace fission-function
kubectl logs --namespace fission deployment/autofission
```

After a successful cycle, the Function's `MaxScale` and the recorded calculation should match. Fission updates the HPA asynchronously, so it may reflect the new limit slightly later.

To run one reconciliation pass from a workstation without starting a daemon and apply any calculated updates:

```bash
autofission --once --context my-cluster
```

Removing the label stops future management. Autofission deliberately does not guess or restore a previous manually configured `MaxScale`; the last calculated value and informational annotations remain until you change them.


## How it works

Each cycle is fail-safe and idempotent:

1. List opted-in Functions, along with Fission Environments, Kubernetes Nodes, and Pods.
2. Keep Ready, uncordoned, non-deleting nodes. Nodes with `NoSchedule` or `NoExecute` taints are excluded by default.
3. Calculate every active, scheduled Pod's effective CPU and memory requests, including regular containers, init containers, restartable init sidecars, Pod-level requests, and Pod overhead. Completed and unbound Pods are excluded from the calculation.
4. For each Function, inherit missing or zero CPU/memory requests from its Environment and add the configured fetcher request. If an existing Function Pod has larger requests than this estimate, use the observed values.
5. Add the requests of that Function's existing Pods back to its capacity budget because step 3 already counted them as workload; this changes only the calculation, not the Pods. Then calculate how many identical Pods fit on each node. Per-node results are summed, so CPU on one node cannot incorrectly combine with memory on another.
6. Apply `max(calculated capacity, MinScale, 1)`, clamp it to the HPA `int32` limit, and merge-patch only drifted Functions. The patch carries the listed `resourceVersion`, so a concurrent user edit produces a conflict instead of a stale overwrite.

A malformed or conflicting managed Function is isolated, so other Functions can still converge; however, the controller remains NotReady until every managed Function completes cleanly. Liveness and readiness files are removed on process start, so a container restart cannot inherit a stale healthy state.


## Configuration

CLI flags take precedence over non-empty environment variables, which take precedence over defaults.

| CLI flag | Environment variable | Helm value | Default |
|---|---|---|---|
| `--interval-seconds` | `AUTOFISSION_INTERVAL_SECONDS` | `controller.intervalSeconds` | `15` |
| `--request-timeout-seconds` | `AUTOFISSION_REQUEST_TIMEOUT_SECONDS` | `controller.requestTimeoutSeconds` | `10` |
| `--retry-attempts` | `AUTOFISSION_RETRY_ATTEMPTS` | `controller.retryAttempts` | `3` |
| `--fetcher-cpu-request` | `AUTOFISSION_FETCHER_CPU_REQUEST` | `controller.fetcherCpuRequest` | `10m` |
| `--fetcher-memory-request` | `AUTOFISSION_FETCHER_MEMORY_REQUEST` | `controller.fetcherMemoryRequest` | `16Mi` |
| `--managed-label` | `AUTOFISSION_MANAGED_LABEL` | `controller.managedLabel` | `autoscaling.fission.io/cluster-capacity` |
| `--managed-value` | `AUTOFISSION_MANAGED_VALUE` | `controller.managedValue` | `true` |
| `--include-tainted-nodes` | `AUTOFISSION_INCLUDE_TAINTED_NODES` | `controller.includeTaintedNodes` | `false` |
| `--log-level` | `AUTOFISSION_LOG_LEVEL` | `controller.logLevel` | `INFO` |
| `--state-directory` | `AUTOFISSION_STATE_DIRECTORY` | — | `/tmp/autofission` (CLI); `/var/run/autofission` (chart) |

`--kubeconfig`, `--context`, and `--in-cluster` select credentials. `--once` runs exactly one pass. `--probe readiness|liveness --max-age-seconds N` is intended for Kubernetes exec probes.


## RBAC and security

The chart grants only these cluster-wide operations:

- `list` Nodes and Pods;
- `list` Fission Environments;
- `list` and `patch` Fission Functions.

It cannot read Secrets, create or delete Functions, or mutate Pods, Nodes, Deployments, or Services. Kubernetes RBAC cannot restrict a `list` or `patch` permission by label, so the opt-in label is an application-level boundary. Listing Pods exposes their specifications, including literal environment-variable values, to the controller process; this permission is necessary to protect capacity already requested by other workloads.

The container runs as UID/GID 65532 with a read-only root filesystem, all Linux capabilities dropped, no privilege escalation, and a RuntimeDefault seccomp profile. A deny-ingress NetworkPolicy is included. Egress is not restricted because a portable policy cannot select every cluster's API endpoint without hard-coded addresses.

The controller has a high PriorityClass so it remains available while function Pods fill the cluster. The separate `autofission-runtime` class is negative and `preemptionPolicy: Never`; configure Fission to use it as shown under installation. Correct resource requests remain essential: Autofission budgets requests, exactly as the scheduler does, rather than measuring live CPU/RAM usage.

Treat permission to set the opt-in label as permission to consume the cluster's elastic budget. In a multi-tenant cluster, restrict that label with your admission policy. Autofission does not implement cross-Function quotas or fair sharing.


## Operations

The chart intentionally runs one replica with a `Recreate` strategy. This prevents two independent writers from racing without adding leader-election privileges. Kubernetes recreates the controller Pod after node or cluster restarts; the controller then discards its health markers and rebuilds state from the API.

Useful checks:

```bash
kubectl rollout status deployment/autofission --namespace fission
kubectl auth can-i list pods \
  --as=system:serviceaccount:fission:autofission --all-namespaces
kubectl auth can-i patch functions.fission.io \
  --as=system:serviceaccount:fission:autofission --all-namespaces
kubectl auth can-i get secrets \
  --as=system:serviceaccount:fission:autofission --all-namespaces
```

The rollout should complete; the Pod-list and Function-patch checks should say `yes`, and the Secret check should say `no`. Helm upgrades are idempotent.

Before uninstalling, remove the opt-in labels and set each Function's `MaxScale` to the limit you want to retain: stopping Autofission does not restore older values.

Uninstalling the release removes its controller resources but intentionally retains the runtime PriorityClass, because Fission may still reference it for future cold starts. It never removes Fission CRDs, Functions, Environments, or namespaces. Remove the retained PriorityClass manually only after changing Fission's `runtimePodSpec`.


## Compatibility and limitations

- Only Fission `newdeploy` Functions are managed. `poolmgr`, empty executor, and `container` are rejected as opt-in configuration errors.
- Every managed Function receives the full capacity it could use by itself. This preserves burst capacity, but simultaneous cold bursts from several Functions can temporarily create Pending Pods. Later cycles account for scheduled peers and reduce limits; Autofission is not a fairness scheduler.
- Fission and Kubernetes require a positive HPA maximum, so capacity below one replica produces `MaxScale=1`. An explicit `MinScale` is honored even when it exceeds currently free capacity.
- The Fission v1 API and Environment resource inheritance are supported and tested with Fission 1.27.0.
- Python 3.8–3.9 use Kubernetes client 27.2–35.x; Python 3.10+ uses client 36.0.3–36.x. Client 36.0.0 is excluded because of its in-cluster authentication regression.
- Capacity is based on CPU, memory, and Pod slots. Ephemeral storage, GPUs and other extended resources, ResourceQuota, topology spread, required affinity, per-function tolerations/node selectors, image architecture, and unscheduled third-party Pending Pods are not modeled.
- Kubernetes in-place Pod resize status is not modeled separately; a request change is reflected after the Pod specification or a replacement Pod exposes the new request.
- Tainted nodes are excluded unless `--include-tainted-nodes` is explicitly set. Only enable it when Fission runtime Pods actually tolerate those taints.
- Extra sidecars and runtime PodSpec overhead are learned from a running function Pod. Before the first replica, the estimate consists of the resolved runtime container plus configured fetcher requests.
- Low, non-preempting priority prevents function Pods from evicting existing workloads through scheduler preemption. It cannot prevent node-pressure eviction caused by inaccurate requests or running nodes at their physical limit; reserve headroom through node allocatable reservations and give ordinary services accurate requests and appropriate priority/QoS.
- The four API lists read during reconciliation do not form an atomic cluster snapshot. Scheduler admission, low runtime priority, resourceVersion preconditions, and repeated idempotent passes provide eventual convergence.
- The controller polls and materializes paginated lists, with a 10,000-page safety bound. Very large clusters should benchmark API-server load and controller memory before shortening the default interval.
- The package does not expose HTTP traffic. External Function requests continue to use Fission's router and whatever Ingress, LoadBalancer, or NodePort you configured for Fission.
- Cluster-scoped RBAC and PriorityClass names are release-prefixed, but a release name reused in another namespace still collides. Install Autofission only once per cluster unless you provide unique `fullnameOverride` and `priorityClasses.*.name` values.


## Troubleshooting

`Autofission is NotReady` — inspect controller logs. A `403` indicates custom RBAC or a ServiceAccount mismatch. `409` means the Function changed after it was listed and is safely retried on the next cycle. Invalid requests, a missing Environment, an unsupported executor, or no eligible Ready nodes are reported with the Function name where possible.

`The calculated limit is smaller than expected` — check cordons, Ready status, taints, Pod requests, Pod slots, fetcher values, and per-node fragmentation. Capacity cannot combine spare CPU and spare memory located on different nodes.

`Function Pods remain Pending` — verify the runtime PriorityClass, taints/tolerations, node selectors, architecture, quota, storage, and concurrent managed Functions. Those constraints can be stricter than Autofission's current capacity model.

`The HPA has not changed yet` — Autofission patches the Function CR. Fission's executor reconciles that change into the HPA asynchronously; controller readiness only confirms the Function patch cycle.


## Development and releases

Repository quality gates include unit, smoke, and typing tests; pinned development tools; Ruff; strict mypy; mutation-test configuration; 100% line and branch coverage; and isolated build checks.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements_dev.txt -e .
ruff check autofission tests
ruff format --check autofission tests
mypy --strict autofission
mypy tests
coverage run -m pytest -m "not e2e"
coverage report --fail-under=100
python -m build
twine check --strict dist/*
helm lint deploy/helm/autofission
```

The end-to-end suite creates a disposable Kind cluster with one cordoned control-plane and three schedulable workers, installs Fission, Metrics Server, and the chart from the current checkout, and runs through real scale-out, scale-in, capacity-contraction, and non-preemption scenarios. Docker, Kind, kubectl, Helm, and the Fission CLI are required locally:

```bash
AUTOFISSION_E2E=1 pytest -m e2e -vv
```

Set `AUTOFISSION_E2E_KEEP_CLUSTER=1` when debugging to preserve the generated cluster. Set `AUTOFISSION_E2E_ARTIFACTS` to choose where failure diagnostics are written. Do not run multiple copies of this suite against the same cluster; every pytest session deliberately creates and owns a separate Kind cluster.

Pushes run lint and the operating-system/Python unit-test matrix. Only after that matrix succeeds, a dedicated Linux job runs the pytest e2e suite against the three-worker Kind cluster. A `v*` tag whose version exactly matches `autofission.__version__` publishes the already tested wheel and sdist to PyPI, a multi-architecture image to GHCR, and the Helm chart as an OCI artifact. Trusted publishing is used for PyPI; no long-lived PyPI token belongs in repository secrets.
