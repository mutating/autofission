from decimal import Decimal, getcontext

import pytest

from autofission.errors import CapacityError
from autofission.quantities import (
    MAX_QUANTITY,
    parse_cpu_millicores,
    parse_memory_bytes,
    parse_pod_count,
    parse_quantity,
)


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('0', Decimal(0)),
        ('+1', Decimal(1)),
        ('-1', Decimal(-1)),
        ('01', Decimal(1)),
        ('1.', Decimal(1)),
        ('.5', Decimal('0.5')),
        ('1.5', Decimal('1.5')),
        ('1n', Decimal('0.000000001')),
        ('1u', Decimal('0.000001')),
        ('1m', Decimal('0.001')),
        ('1k', Decimal(1_000)),
        ('1K', Decimal(1_000)),
        ('1M', Decimal(1_000_000)),
        ('1G', Decimal(1_000_000_000)),
        ('1T', Decimal(10**12)),
        ('1P', Decimal(10**15)),
        ('1E', Decimal(10**18)),
        ('1Ki', Decimal(2**10)),
        ('1Mi', Decimal(2**20)),
        ('1Gi', Decimal(2**30)),
        ('1Ti', Decimal(2**40)),
        ('1Pi', Decimal(2**50)),
        ('1Ei', Decimal(2**60)),
        ('1e3', Decimal(1_000)),
        ('1E+3', Decimal(1_000)),
        ('1e-3', Decimal('0.001')),
    ],
)
def test_parse_quantity_supports_kubernetes_grammar(
    source: str,
    expected: Decimal,
) -> None:
    assert parse_quantity(source) == expected


@pytest.mark.parametrize(
    'source',
    [
        '',
        ' ',
        ' 1',
        '1 ',
        '.',
        '+',
        '-',
        '1..0',
        '--1',
        'NaN',
        'Infinity',
        '1KiB',
        '1ki',
        '1KB',
        '1foo',
        '1e',
        '1e+',
        '1_000',
        '0x10',
        '١',  # noqa: RUF001 - deliberately invalid Unicode quantity
        '１２',  # noqa: RUF001 - deliberately invalid Unicode quantity
        'x' * 129,
    ],
)
def test_parse_quantity_rejects_invalid_text(source: str) -> None:
    with pytest.raises(CapacityError):
        parse_quantity(source)


@pytest.mark.parametrize('source', [None, True, 1, 1.0, Decimal(1), [], {}])
def test_parse_quantity_rejects_non_strings(source: object) -> None:
    with pytest.raises(CapacityError, match='must be a string'):
        parse_quantity(source)  # type: ignore[arg-type]


@pytest.mark.parametrize('source', ['1e100000', '1e99999', '1e10001', '1e-10001'])
def test_parse_quantity_rejects_exponent_bombs(source: str) -> None:
    with pytest.raises(CapacityError, match='exponent is too large'):
        parse_quantity(source)


@pytest.mark.parametrize('source', [str(MAX_QUANTITY + 1), '8Ei', '-8Ei'])
def test_parse_quantity_rejects_values_outside_int64(source: str) -> None:
    with pytest.raises(CapacityError, match='exceeds int64'):
        parse_quantity(source)


def test_parsing_is_independent_of_global_decimal_precision() -> None:
    original = getcontext().prec
    try:
        getcontext().prec = 6
        assert parse_quantity('123456789.123') == Decimal('123456789.123')
    finally:
        getcontext().prec = original


def test_parsing_is_independent_of_global_decimal_exponent_bounds() -> None:
    context = getcontext()
    original = (context.Emax, context.Emin)
    try:
        context.Emax = 9
        context.Emin = -9
        with pytest.raises(CapacityError, match='exceeds int64'):
            parse_quantity('1e1000')
        assert parse_quantity('1e-1000') == Decimal('1e-1000')
    finally:
        context.Emax, context.Emin = original


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('0', 0),
        ('1n', 1),
        ('100u', 1),
        ('.1m', 1),
        ('1m', 1),
        ('1.0001', 1001),
        ('250m', 250),
    ],
)
def test_cpu_is_rounded_up_to_millicores(source: str, expected: int) -> None:
    assert parse_cpu_millicores(source) == expected


@pytest.mark.parametrize(
    ('source', 'expected'),
    [('0', 0), ('1n', 1), ('.1', 1), ('1.1', 2), ('1', 1), ('128Mi', 128 * 2**20)],
)
def test_memory_is_rounded_up_to_bytes(source: str, expected: int) -> None:
    assert parse_memory_bytes(source) == expected


@pytest.mark.parametrize('parser', [parse_cpu_millicores, parse_memory_bytes])
def test_resource_quantities_reject_negative_values(parser: object) -> None:
    with pytest.raises(CapacityError, match='cannot be negative'):
        parser('-1')  # type: ignore[operator]


def test_cpu_scheduler_units_cannot_overflow() -> None:
    with pytest.raises(CapacityError, match='scheduler range'):
        parse_cpu_millicores(str(MAX_QUANTITY))


@pytest.mark.parametrize(('source', 'expected'), [('110', 110), ('1k', 1_000), ('0', 0)])
def test_parse_pod_count(source: str, expected: int) -> None:
    assert parse_pod_count(source) == expected


@pytest.mark.parametrize('source', ['-1', '.5'])
def test_parse_pod_count_rejects_negative_or_fractional_values(source: str) -> None:
    with pytest.raises(CapacityError, match='non-negative integer'):
        parse_pod_count(source)
