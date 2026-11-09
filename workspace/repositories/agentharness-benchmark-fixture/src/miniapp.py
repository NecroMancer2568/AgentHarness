"""Tiny utility module with intentionally seeded defects for harness smoke tests."""


def is_even(value: int) -> bool:
    """Return True when value is even."""
    return value % 2 == 1  # seeded bug: parity is inverted


def normalize_email(email: str) -> str:
    """Trim surrounding whitespace and normalize an email address to lowercase."""
    return email.lower()  # seeded bug: surrounding whitespace is retained


def safe_divide(numerator: float, denominator: float) -> float:
    """Divide two numbers; raise ValueError for a zero denominator."""
    return numerator / denominator  # seeded bug: zero raises ZeroDivisionError


def unique_preserving_order(values: list[int]) -> list[int]:
    """Return unique values in first-seen order."""
    return sorted(set(values))  # seeded bug: original order is lost
