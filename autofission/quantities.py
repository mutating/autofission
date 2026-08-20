"""Strict conversion of Kubernetes quantities to scheduler units."""

from __future__ import annotations

import re
from decimal import MAX_EMAX, MIN_EMIN, ROUND_CEILING, Decimal, localcontext

from autofission.errors import CapacityError

MAX_QUANTITY = 2**63 - 1
MAX_QUANTITY_LENGTH = 128
MAX_ABSOLUTE_EXPONENT = 10_000

_QUANTITY_PATTERN = re.compile(
    r'^(?P<number>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))'
    r'(?P<suffix>[eE][+-]?[0-9]+|[KMGTPE]i|[numkKMGTP E]?)$'.replace(' ', ''),
    re.ASCII,
)

_DECIMAL_MULTIPLIERS = {
    '': 1,
    'n': 10**-9,
    'u': 10**-6,
    'm': 10**-3,
    'k': 10**3,
    'K': 10**3,
    'M': 10**6,
    'G': 10**9,
    'T': 10**12,
    'P': 10**15,
    'E': 10**18,
}

_BINARY_MULTIPLIERS = {
    'Ki': 2**10,
    'Mi': 2**20,
    'Gi': 2**30,
    'Ti': 2**40,
    'Pi': 2**50,
    'Ei': 2**60,
}


def parse_quantity(value: str) -> Decimal:
    """Parse the Kubernetes Quantity grammar without accepting Python coercions."""
    if not isinstance(value, str):
        raise CapacityError('a Kubernetes quantity must be a string')
    if not value:
        raise CapacityError(f'invalid Kubernetes quantity: {value!r}')
    if len(value) > MAX_QUANTITY_LENGTH:
        raise CapacityError(
            f'Kubernetes quantity is longer than {MAX_QUANTITY_LENGTH} characters',
        )

    match = _QUANTITY_PATTERN.fullmatch(value)
    if match is None:
        raise CapacityError(f'invalid Kubernetes quantity: {value!r}')

    number = match.group('number')
    suffix = match.group('suffix')
    digits = sum(character.isdigit() for character in number)

    with localcontext() as context:
        context.prec = max(digits + 32, 64)
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        quantity = Decimal(number)
        if suffix in _BINARY_MULTIPLIERS:
            quantity *= _BINARY_MULTIPLIERS[suffix]
        elif suffix in _DECIMAL_MULTIPLIERS:
            quantity *= Decimal(str(_DECIMAL_MULTIPLIERS[suffix]))
        else:
            exponent_text = suffix[1:]
            if len(exponent_text.lstrip('+-')) > 5:
                raise CapacityError(f'quantity exponent is too large: {value!r}')
            exponent = int(exponent_text)
            if abs(exponent) > MAX_ABSOLUTE_EXPONENT:
                raise CapacityError(f'quantity exponent is too large: {value!r}')
            quantity *= Decimal(10) ** exponent

    if quantity.copy_abs() > MAX_QUANTITY:
        raise CapacityError(f'Kubernetes quantity exceeds int64: {value!r}')
    return quantity


def _positive_scheduler_units(value: str, multiplier: int, resource: str) -> int:
    quantity = parse_quantity(value)
    if quantity < 0:
        raise CapacityError(f'{resource} quantity cannot be negative: {value!r}')
    units = int((quantity * multiplier).to_integral_value(rounding=ROUND_CEILING))
    if units > MAX_QUANTITY:
        raise CapacityError(f'{resource} quantity exceeds scheduler range: {value!r}')
    return units


def parse_cpu_millicores(value: str) -> int:
    """Convert a CPU quantity to the scheduler's integer millicores."""
    return _positive_scheduler_units(value, 1_000, 'CPU')


def parse_memory_bytes(value: str) -> int:
    """Convert a memory quantity to the scheduler's integer bytes."""
    return _positive_scheduler_units(value, 1, 'memory')


def parse_pod_count(value: str) -> int:
    """Parse an allocatable pod count, rejecting fractional values."""
    quantity = parse_quantity(value)
    if quantity < 0 or quantity != quantity.to_integral_value():
        raise CapacityError(f'pod count must be a non-negative integer: {value!r}')
    return int(quantity)
