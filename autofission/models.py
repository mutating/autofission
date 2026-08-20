"""Small immutable models shared by Autofission components."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Resources:
    """CPU and memory in Kubernetes scheduler units."""

    cpu_millicores: int = 0
    memory_bytes: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ('cpu_millicores', self.cpu_millicores),
            ('memory_bytes', self.memory_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f'{name} must be an integer')

    def __add__(self, other: Resources) -> Resources:
        return Resources(
            self.cpu_millicores + other.cpu_millicores,
            self.memory_bytes + other.memory_bytes,
        )

    def __sub__(self, other: Resources) -> Resources:
        return Resources(
            self.cpu_millicores - other.cpu_millicores,
            self.memory_bytes - other.memory_bytes,
        )

    def maximum(self, other: Resources) -> Resources:
        """Return the component-wise maximum of two resource vectors."""
        return Resources(
            max(self.cpu_millicores, other.cpu_millicores),
            max(self.memory_bytes, other.memory_bytes),
        )


@dataclass(frozen=True)
class NodeResources:
    """Allocatable resources of one eligible node."""

    name: str
    allocatable: Resources
    pod_slots: int


@dataclass(frozen=True)
class ReconcileResult:
    """Observable outcome of one complete reconciliation pass."""

    managed_functions: int
    updated_functions: int
    ready_nodes: int
