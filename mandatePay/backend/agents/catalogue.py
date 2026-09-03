"""Catalogue discovery for the buyer agent.

Every product the agent can ever consider comes from here, and everything
here comes from a database row via the same query the public `/search`
endpoint uses. The model is not asked what exists; it is shown what
exists.

That is what makes "the AI cannot fabricate a product and buy it" a
structural property rather than a hope: there is no path from model output
to this module's return value.
"""

from typing import Any

from sqlalchemy.orm import Session

from backend.commerce.product_routes import search_products
from backend.databases.product_db import product_db
from backend.models.Intent import Intent

# Enough breadth for a demo catalogue without handing the ranker an
# unbounded prompt.
MAX_DISCOVERED = 40

# Words too short or too generic to narrow anything usefully.
_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "buy", "can", "for", "get", "good",
        "have", "her", "him", "his", "its", "me", "my", "need", "new", "one",
        "our", "please", "some", "that", "the", "them", "they", "this", "to",
        "want", "with", "you", "your",
    }
)


def _as_dict(row: product_db) -> dict[str, Any]:
    """The catalogue fields the selection layer is allowed to see."""
    return {
        "product_id": row.product_id,
        "product_name": row.product_name,
        "brand": row.brand,
        "category": row.category,
        "cost": row.cost,
        "quantity": row.quantity,
        "merchant_id": row.merchant_id,
    }


def _tokens(text: str) -> list[str]:
    return [
        word
        for word in "".join(
            character if character.isalnum() else " " for character in text.lower()
        ).split()
        if len(word) > 2 and word not in _STOP_WORDS
    ]


def discover_candidates(db: Session, intent: Intent) -> list[dict[str, Any]]:
    """Find catalogue rows plausibly matching the requested item.

    Recall-first on purpose. This step only has to produce a superset of
    what the buyer might have meant; the hard-constraint filter that runs
    next is what decides eligibility, and it does so deterministically.
    Being generous here and strict there is safer than the reverse.

    Note what is deliberately NOT passed to the query: the buyer's budget.
    Filtering it away in SQL would hide from the hard filter -- and from
    the response -- the fact that a product was excluded on price. The
    budget is enforced by the server either way; this keeps the reason
    visible.
    """
    item = intent.item.strip()
    seen: dict[Any, dict[str, Any]] = {}

    def collect(rows: list[product_db]) -> None:
        for row in rows:
            if row.product_id not in seen and len(seen) < MAX_DISCOVERED:
                seen[row.product_id] = _as_dict(row)

    if item:
        collect(search_products(db, item=item))

    # The whole phrase rarely appears verbatim in a product name, so widen
    # to the individual words the buyer used.
    if len(seen) < MAX_DISCOVERED:
        for token in _tokens(item):
            collect(search_products(db, item=token))

    # A brand the buyer named is worth searching on its own -- "Logitech
    # only" tells us where to look even when the item word misses.
    if len(seen) < MAX_DISCOVERED:
        for brand in (intent.required_brand, intent.preferred_brand):
            if brand:
                collect(
                    db.query(product_db)
                    .filter(product_db.brand.ilike(f"%{brand.strip()}%"))
                    .all()
                )

    return list(seen.values())
