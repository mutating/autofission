from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from hypothesis import given
from hypothesis import strategies as st

from autofission.capacity import ClusterSnapshot, pod_requests
from autofission.models import Resources
from autofission.quantities import parse_cpu_millicores, parse_quantity
from tests.helpers import container, node


@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_decimal_milli_quantity_matches_independent_decimal_oracle(value: int) -> None:
    assert parse_quantity(f'{value}m') == Decimal(value) / 1_000


@given(
    whole=st.integers(min_value=0, max_value=1_000_000),
    fraction=st.integers(min_value=0, max_value=999_999),
)
def test_cpu_rounding_matches_ceiling_oracle(whole: int, fraction: int) -> None:
    source = f'{whole}.{fraction:06d}'
    expected = int(
        (Decimal(source) * 1_000).to_integral_value(rounding=ROUND_CEILING),
    )
    assert parse_cpu_millicores(source) == expected


@given(
    capacities=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=8_000),
            st.integers(min_value=1, max_value=8_000),
            st.integers(min_value=0, max_value=100),
        ),
        min_size=1,
        max_size=20,
    ),
    cpu_request=st.integers(min_value=1, max_value=2_000),
    memory_request=st.integers(min_value=1, max_value=2_000),
)
def test_per_node_capacity_matches_integer_bin_packing_oracle(
    capacities: list[tuple[int, int, int]],
    cpu_request: int,
    memory_request: int,
) -> None:
    nodes = [
        node(
            f'node-{index}',
            cpu=f'{cpu}m',
            memory=str(memory),
            pods=str(pods),
        )
        for index, (cpu, memory, pods) in enumerate(capacities)
    ]
    expected = sum(
        min(cpu // cpu_request, memory // memory_request, pods) for cpu, memory, pods in capacities
    )

    snapshot = ClusterSnapshot.build(nodes, [])

    assert (
        snapshot.function_capacity(
            'function',
            Resources(cpu_request, memory_request),
        )
        == expected
    )
    assert (
        ClusterSnapshot.build(reversed(nodes), []).function_capacity(
            'function',
            Resources(cpu_request, memory_request),
        )
        == expected
    )


@given(
    requests=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=2_000),
            st.integers(min_value=0, max_value=2_000),
        ),
        min_size=1,
        max_size=20,
    ),
)
def test_regular_container_request_sum_is_permutation_invariant(
    requests: list[tuple[int, int]],
) -> None:
    containers = [container(f'{cpu}m', str(memory)) for cpu, memory in requests]
    expected = Resources(
        sum(cpu for cpu, _ in requests),
        sum(memory for _, memory in requests),
    )

    assert pod_requests({'containers': containers}) == expected
    assert pod_requests({'containers': list(reversed(containers))}) == expected
