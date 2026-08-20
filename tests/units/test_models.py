import pytest

from autofission.models import Resources


def test_resource_arithmetic_and_component_maximum() -> None:
    first = Resources(10, 20)
    second = Resources(3, 30)

    assert first + second == Resources(13, 50)
    assert first - second == Resources(7, -10)
    assert first.maximum(second) == Resources(10, 30)


@pytest.mark.parametrize('value', [True, 1.0, '1'])
def test_resources_reject_non_integer_components(value: object) -> None:
    with pytest.raises(TypeError, match='must be an integer'):
        Resources(value, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match='must be an integer'):
        Resources(1, value)  # type: ignore[arg-type]
