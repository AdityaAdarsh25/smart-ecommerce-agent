"""Natural language in, structured `Intent` out.

This is the AI half of the system doing the only job it is good at:
resolving ambiguity in human phrasing. It reads the buyer's sentence and
says what the buyer appears to want.

What it produces is a *wish*, not an authorization. Every field here is
later either checked by the server against catalogue rows it read itself
(budget, brand, category, stock) or used only for ranking. No field on
`Intent` can move money, and nothing on it is trusted as fact about the
world -- `max_budget` is what the buyer asked for, never what something
costs.
"""

from typing import Any, Optional
from uuid import UUID

from backend.agents.llm_client import LLMCallable, LLMResponseError
from backend.models.Intent import Intent

MAX_MESSAGE_LENGTH = 1000
MAX_LIST_ITEMS = 8
MAX_FIELD_LENGTH = 120

SYSTEM_PROMPT = """\
You are the intent parser for MandatePay, an agentic commerce gateway.

Your ONLY job is to read one shopping request written by a buyer and
describe it as structured JSON. You do not choose products, you do not
price anything, and you have no authority over spending. Prices, limits,
approvals and payment are decided by server-side code you cannot reach.

Return a single JSON object with exactly these keys:

  "item"                  string  the single product type requested, in
                                  plain lowercase words (e.g. "wireless
                                  keyboard"). Empty string if the buyer
                                  never named a product type.
  "quantity"              integer how many units. 1 unless the buyer said
                                  otherwise.
  "max_budget"            number or null. ONLY if the buyer stated a
                                  spending ceiling. This is the buyer's
                                  stated limit, never a product price.
  "required_brand"        string or null. Only when the buyer made a brand
                                  non-negotiable ("Logitech only", "must
                                  be Logitech").
  "required_category"     string or null. Only when the buyer named a
                                  catalogue category as non-negotiable.
  "hard_constraints"      array of short lowercase strings the buyer stated
                                  as absolute requirements ("wireless",
                                  "black"). Each must be a single word or
                                  short phrase that could plausibly appear
                                  in a product name.
  "preferred_brand"       string or null. A leaning, not a requirement
                                  ("preferably Logitech").
  "soft_preferences"      array of short lowercase strings describing
                                  leanings, not requirements.
  "is_multi_item"         boolean true if the buyer asked for two or more
                                  DIFFERENT products in one request
                                  ("a keyboard and a mouse"). Several units
                                  of ONE product is NOT multi-item.
  "requested_items"       array of the distinct product types requested.
  "needs_clarification"   boolean true if the request is too vague or
                                  contradictory to act on.
  "clarification_question" string or null. The single question you would
                                  ask the buyer.

Rules:
- Never invent a constraint the buyer did not state.
- Never invent product attributes. Only report words the buyer used.
- "under X", "below X", "at most X", "no more than X" are max_budget.
- "preferably", "ideally", "I'd like" are soft. "must", "only", "has to
  be" are hard.
- A phrase saying why or where something will be used ("for work", "for
  the balcony") is context. It is never a hard constraint.
- Output JSON only. No prose, no code fences.
"""


def _as_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LLMResponseError("Expected a string field in the parsed intent.")
    trimmed = value.strip()[:MAX_FIELD_LENGTH]
    return trimmed or None


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LLMResponseError("Expected a list field in the parsed intent.")
    items: list[str] = []
    for entry in value[:MAX_LIST_ITEMS]:
        if not isinstance(entry, str):
            raise LLMResponseError("Intent list entries must be strings.")
        trimmed = entry.strip()[:MAX_FIELD_LENGTH]
        if trimmed:
            items.append(trimmed)
    return items


def _as_quantity(value: Any) -> int:
    """Quantity must be a whole number of units, at least one.

    Rejected rather than clamped: silently turning a nonsensical quantity
    into 1 would quote a purchase the buyer never described.
    """
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMResponseError("Intent quantity must be a number.")
    if isinstance(value, float) and not value.is_integer():
        raise LLMResponseError("Intent quantity must be a whole number.")
    quantity = int(value)
    if quantity < 1:
        raise LLMResponseError("Intent quantity must be at least 1.")
    return quantity


def _as_optional_budget(value: Any) -> Optional[float]:
    """The buyer's stated ceiling. A ceiling of zero or less is not a
    budget, it is a malformed reading, and quoting against it would be
    guesswork."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMResponseError("Intent max_budget must be a number or null.")
    budget = float(value)
    if budget <= 0:
        raise LLMResponseError("Intent max_budget must be greater than zero.")
    return budget


def _as_bool(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise LLMResponseError("Expected a boolean field in the parsed intent.")
    return value


def intent_from_payload(
    payload: dict[str, Any],
    *,
    buyer_id: UUID,
    raw_text: str,
) -> Intent:
    """Validate a model reply into an `Intent`, or fail.

    Every field is coerced explicitly and a wrong shape raises. There is
    no lenient path: a reply we cannot read is an error, never a set of
    defaults we quietly proceed on.

    `buyer_id` and `raw_text` come from the request, not the model. The
    model cannot nominate whose money is being considered.
    """
    if not isinstance(payload, dict):
        raise LLMResponseError("The intent parser did not return a JSON object.")

    item = payload.get("item")
    if item is not None and not isinstance(item, str):
        raise LLMResponseError("Intent item must be a string.")

    requested_items = _as_str_list(payload.get("requested_items"))
    is_multi_item = _as_bool(payload.get("is_multi_item"))

    # Belt and braces: two distinct named items is multi-item whatever the
    # boolean says. Under-reporting this flag is the dangerous direction,
    # because it is what would let a basket be split across transactions.
    if len({entry.lower() for entry in requested_items}) > 1:
        is_multi_item = True

    return Intent(
        buyer_id=buyer_id,
        raw_text=raw_text,
        item=(item or "").strip()[:MAX_FIELD_LENGTH],
        quantity=_as_quantity(payload.get("quantity")),
        max_budget=_as_optional_budget(payload.get("max_budget")),
        required_brand=_as_optional_str(payload.get("required_brand")),
        required_category=_as_optional_str(payload.get("required_category")),
        hard_constraints=_as_str_list(payload.get("hard_constraints")),
        preferred_brand=_as_optional_str(payload.get("preferred_brand")),
        preferred_merchant=_as_optional_str(payload.get("preferred_merchant")),
        soft_preferences=_as_str_list(payload.get("soft_preferences")),
        is_multi_item=is_multi_item,
        requested_items=requested_items,
        needs_clarification=_as_bool(payload.get("needs_clarification")),
        clarification_question=_as_optional_str(payload.get("clarification_question")),
    )


def parse_intent(
    *,
    llm: LLMCallable,
    buyer_id: UUID,
    message: str,
) -> Intent:
    """Ask the model to read one buyer request, then validate the answer."""
    text = message.strip()
    if not text:
        raise LLMResponseError("The buyer message is empty.")

    payload = llm(
        system=SYSTEM_PROMPT,
        user=f"Buyer request:\n{text[:MAX_MESSAGE_LENGTH]}",
    )
    return intent_from_payload(payload, buyer_id=buyer_id, raw_text=text)
