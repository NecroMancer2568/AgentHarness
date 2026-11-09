import pytest
from src.miniapp import safe_divide


def test_safe_divide_regular_values():
    assert safe_divide(9, 3) == 3
    assert safe_divide(-8, 2) == -4


def test_zero_denominator_raises_value_error():
    with pytest.raises(ValueError, match="zero"):
        safe_divide(1, 0)
