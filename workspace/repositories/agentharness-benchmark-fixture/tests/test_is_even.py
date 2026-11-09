from src.miniapp import is_even


def test_even_and_odd_values():
    assert is_even(0) is True
    assert is_even(2) is True
    assert is_even(-4) is True
    assert is_even(1) is False
    assert is_even(-3) is False
