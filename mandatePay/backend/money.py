"""Exact money handling.

Financial amounts are held as integer paise and exposed to Python as
`Decimal` rupees. Nothing in the quote/payment path performs a binary
floating-point multiplication:

    quoted_total_paise = quoted_unit_price_paise * quantity

is exact integer arithmetic, and the Razorpay amount is that same integer.

The legacy `products.cost`, `buyers.balance` and `transactions.amount`
columns are still `Float`; converting them goes through `to_decimal`,
which routes via `str()` so that the value a human seeded (100.0) becomes
Decimal("100.0") rather than the binary artefact of the same float.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Union

from sqlalchemy import Integer
from sqlalchemy.types import TypeDecorator

PAISE_PER_RUPEE = 100

_ONE = Decimal("1")
_CENT = Decimal("0.01")

Moneyish = Union[Decimal, int, float, str]


def to_decimal(value: Moneyish) -> Decimal:
    """Coerce a rupee amount to Decimal without inheriting float noise."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def to_paise(value: Moneyish) -> int:
    """Rupees -> integer paise. The canonical form for comparison and for
    the amount handed to Razorpay."""
    return int((to_decimal(value) * PAISE_PER_RUPEE).quantize(_ONE, rounding=ROUND_HALF_UP))


def from_paise(paise: int) -> Decimal:
    """Integer paise -> Decimal rupees, always at 2 decimal places."""
    return (Decimal(int(paise)) / PAISE_PER_RUPEE).quantize(_CENT)


class MoneyPaise(TypeDecorator):
    """Stores rupees as integer paise; reads back as Decimal rupees.

    Chosen over `Numeric` because SQLite has no native decimal type --
    SQLAlchemy would round-trip a Numeric column through a float and warn
    about the lost precision. An integer column has neither problem.
    """

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return to_paise(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return from_paise(value)
