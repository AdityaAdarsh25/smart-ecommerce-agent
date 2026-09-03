"""Package 4 -- catalogue discovery, hard filtering and selection.

The property under test throughout: hard constraints FILTER, soft
preferences only RANK, and the model may only ever nominate something out
of the set the server built.
"""

import uuid

from backend.databases.quote_db import quote_db


def shown_names(fake_llm):
    return {candidate["product_name"] for candidate in fake_llm.candidates_shown()}


# --- 7. budget filters deterministically, server-side ----------------------


def test_max_budget_filters_candidates_before_the_model_sees_them(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="wireless keyboard", quantity=1, max_budget=3000,
        hard_constraints=["wireless"],
    )
    agent_purchase("wireless keyboard under 3000")

    shown = fake_llm.candidates_shown()
    assert shown, "the ranker should have been given the survivors"
    # The model is never asked whether a price fits a budget; the server
    # has already compared the numbers itself.
    assert all(candidate["unit_price_inr"] <= 3000 for candidate in shown)
    assert "Logitech Wireless Keyboard MX Keys" not in shown_names(fake_llm)


def test_budget_applies_to_the_total_not_the_unit_price(
    fake_llm, agent_catalogue, agent_purchase
):
    """Two units at 1800 is 3600, which is over a 3000 ceiling."""
    fake_llm.set_intent(
        item="wireless keyboard", quantity=2, max_budget=3000,
        hard_constraints=["wireless"],
    )
    agent_purchase("two wireless keyboards, 3000 total")

    assert "Dell Wireless Keyboard KB500" not in shown_names(fake_llm)
    assert "Logitech Wireless Keyboard K120" in shown_names(fake_llm)


def test_a_free_text_hard_constraint_filters_on_catalogue_text(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="keyboard", quantity=1, max_budget=3000, hard_constraints=["wireless"]
    )
    agent_purchase("must be wireless, under 3000")

    assert "Dell Wired Keyboard KB216" not in shown_names(fake_llm)


def test_stock_is_a_hard_constraint(fake_llm, agent_catalogue, agent_purchase):
    """Only one Corsair unit exists, so it cannot satisfy a request for two."""
    fake_llm.set_intent(
        item="wireless keyboard", quantity=2, max_budget=10000,
        hard_constraints=["wireless"],
    )
    agent_purchase("two wireless keyboards")

    assert "Corsair Wireless Keyboard K70" not in shown_names(fake_llm)


# --- 8. a hard-invalid product cannot win on soft preferences --------------


def test_a_hard_invalid_product_cannot_win_soft_ranking(
    fake_llm, agent_catalogue, agent_purchase
):
    """The over-budget MX Keys is the best match on the stated preferences
    and the model is told to pick it. It still cannot be bought."""
    fake_llm.set_intent(
        item="wireless keyboard",
        quantity=1,
        max_budget=3000,
        hard_constraints=["wireless"],
        preferred_brand="Dell",
        soft_preferences=["mx keys", "premium"],
    )
    fake_llm.select_product(
        agent_catalogue["premium_id"], rationale="best on every preference"
    )

    body = agent_purchase("wireless keyboard under 3000, ideally Dell, mx keys style")

    assert "Logitech Wireless Keyboard MX Keys" not in shown_names(fake_llm)
    assert body["selection"]["product"]["product_id"] != agent_catalogue["premium_id"]
    assert float(body["quote"]["quoted_total"]) <= 3000
    assert any("not among the eligible candidates" in note for note in body["notes"])


# --- 9. the nominated product must exist in the server catalogue ----------


def test_a_hallucinated_product_id_is_discarded(
    fake_llm, agent_catalogue, agent_purchase, db_session
):
    invented = str(uuid.uuid4())
    fake_llm.set_intent(
        item="wireless keyboard", quantity=1, max_budget=3000,
        hard_constraints=["wireless"], preferred_brand="Dell",
    )
    fake_llm.select_product(invented, rationale="a product that does not exist")

    body = agent_purchase("wireless keyboard under 3000, ideally Dell")

    assert body["selection"]["product"]["product_id"] != invented
    assert body["notes"]
    # Whatever was quoted is a real catalogue row.
    quoted_id = body["quote"]["product"]["product_id"]
    assert quoted_id in {
        agent_catalogue["cheap_id"],
        agent_catalogue["mid_id"],
        agent_catalogue["rival_id"],
        agent_catalogue["scarce_id"],
    }


def test_a_non_uuid_product_id_is_discarded(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="wireless keyboard", quantity=1, max_budget=3000,
        hard_constraints=["wireless"], preferred_brand="Dell",
    )
    fake_llm.select_product("the-cheapest-one")

    body = agent_purchase("wireless keyboard under 3000, ideally Dell")

    assert body["selection"] is not None
    assert any("valid product id" in note for note in body["notes"])


def test_a_discarded_selection_falls_back_to_asking_when_nothing_clearly_wins(
    fake_llm, agent_catalogue, agent_purchase, db_session
):
    """The fallback is the deterministic engine, not a guess. With no
    preference to discriminate on it asks rather than picking one."""
    fake_llm.set_intent(
        item="wireless keyboard", quantity=1, max_budget=3000,
        hard_constraints=["wireless"],
    )
    fake_llm.select_product(agent_catalogue["premium_id"])

    body = agent_purchase("wireless keyboard under 3000")

    assert body["selection_action"] == "CLARIFY"
    assert body["quote"] is None
    assert db_session.query(quote_db).count() == 0


# --- 10. the nominated product must be in the eligible survivor set -------


def test_a_real_but_filtered_out_product_is_discarded(
    fake_llm, agent_catalogue, agent_purchase
):
    """The wired keyboard is a genuine catalogue row, and cheap. It failed
    the 'wireless' hard constraint, so it is not on the table."""
    fake_llm.set_intent(
        item="keyboard", quantity=1, max_budget=3000,
        hard_constraints=["wireless"], preferred_brand="Dell",
    )
    fake_llm.select_product(agent_catalogue["wired_id"], rationale="cheapest of all")

    body = agent_purchase("must be wireless, under 3000, ideally Dell")

    assert body["quote"]["product"]["product_id"] != agent_catalogue["wired_id"]
    assert "Dell Wired Keyboard KB216" not in shown_names(fake_llm)


def test_the_selection_reports_how_many_candidates_were_eligible(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="wireless keyboard", quantity=1, max_budget=3000,
        hard_constraints=["wireless"],
    )
    body = agent_purchase("wireless keyboard under 3000")

    assert body["selection"]["eligible_candidates"] == len(
        fake_llm.candidates_shown()
    )


# --- 11 & 12. selection when there is a clear winner ----------------------


def test_precise_request_with_a_clear_best_candidate_selects(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="wireless keyboard",
        quantity=1,
        max_budget=3000,
        hard_constraints=["wireless"],
        preferred_brand="Logitech",
    )
    fake_llm.select_product(
        agent_catalogue["cheap_id"], rationale="cheapest Logitech that fits"
    )

    body = agent_purchase("wireless keyboard under 3000, preferably Logitech")

    assert body["selection_action"] == "PROPOSE"
    assert body["selection"]["product"]["product_id"] == agent_catalogue["cheap_id"]
    assert body["selection"]["rationale"] == "cheapest Logitech that fits"
    assert body["quote"] is not None


def test_multiple_candidates_still_select_when_one_clearly_wins(
    fake_llm, agent_catalogue, agent_purchase
):
    """Several products survive the hard filter. That is not ambiguity --
    the buyer said enough to pick between them."""
    fake_llm.set_intent(
        item="wireless keyboard",
        quantity=1,
        max_budget=3000,
        hard_constraints=["wireless"],
        preferred_brand="Logitech",
        soft_preferences=["logitech"],
    )
    fake_llm.select_product(agent_catalogue["mid_id"], rationale="best Logitech")

    body = agent_purchase("wireless keyboard under 3000, preferably Logitech")

    assert body["selection"]["eligible_candidates"] > 1
    assert body["selection_action"] == "PROPOSE"
    assert body["selection"]["product"]["product_id"] == agent_catalogue["mid_id"]
    assert body["selection"]["alternatives"]


# --- 13. one accidental match does not make a vague request precise -------


def test_vague_request_with_exactly_one_candidate_still_clarifies(
    fake_llm, seed, agent_purchase, db_session
):
    """The catalogue contains exactly one 'widget'. The buyer still never
    said they wanted it."""
    fake_llm.set_intent(item="widget", quantity=1)

    body = agent_purchase("buy me something useful")

    assert body["selection_action"] == "CLARIFY"
    assert body["next_action"] == "clarify"
    assert body["quote"] is None
    assert db_session.query(quote_db).count() == 0
    # Nothing was even ranked -- ambiguity was settled from the request.
    assert fake_llm.ranking_calls == []


def test_vague_request_with_many_candidates_also_clarifies(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(item="keyboard", quantity=1)

    body = agent_purchase("buy me a keyboard")

    assert body["selection_action"] == "CLARIFY"
    assert body["quote"] is None


def test_vague_request_with_no_candidates_still_clarifies_rather_than_no_match(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(item="submarine", quantity=1)

    body = agent_purchase("buy me something useful")

    assert body["selection_action"] == "CLARIFY"


# --- 14. material ambiguity flagged by the parser -------------------------


def test_materially_ambiguous_request_clarifies(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="keyboard",
        quantity=1,
        max_budget=3000,
        needs_clarification=True,
        clarification_question="Wireless or wired?",
    )

    body = agent_purchase("get me a keyboard, whatever is good")

    assert body["selection_action"] == "CLARIFY"
    assert body["clarification_question"] == "Wireless or wired?"


def test_the_model_may_ask_when_survivors_are_interchangeable(
    fake_llm, agent_catalogue, agent_purchase, db_session
):
    fake_llm.set_intent(
        item="wireless keyboard", quantity=1, max_budget=3000,
        hard_constraints=["wireless"],
    )
    fake_llm.ask_to_clarify("Do you prefer Logitech or Dell?")

    body = agent_purchase("wireless keyboard under 3000")

    assert body["selection_action"] == "CLARIFY"
    assert body["clarification_question"] == "Do you prefer Logitech or Dell?"
    assert db_session.query(quote_db).count() == 0


# --- 15. clarification never produces a quote -----------------------------


def test_clarification_creates_no_quote_and_no_transaction(
    fake_llm, agent_catalogue, agent_purchase, db_session, fake_razorpay
):
    from backend.databases.transaction_db import transaction_db

    fake_llm.set_intent(item="keyboard", quantity=1)
    body = agent_purchase("buy me a keyboard")

    assert body["quote"] is None
    assert body["policy"] is None
    assert body["selection"] is None
    assert db_session.query(quote_db).count() == 0
    assert db_session.query(transaction_db).count() == 0
    assert fake_razorpay.orders_created == []


# --- 16. multi-item requests are refused, never split --------------------


def test_multi_item_request_is_refused_and_creates_no_quote(
    fake_llm, agent_catalogue, agent_purchase, db_session
):
    fake_llm.set_intent(
        item="keyboard",
        quantity=1,
        is_multi_item=True,
        requested_items=["keyboard", "mouse"],
    )

    body = agent_purchase("Buy me a keyboard and a mouse")

    assert body["selection_action"] == "UNSUPPORTED"
    assert body["next_action"] == "stop"
    assert body["quote"] is None
    assert db_session.query(quote_db).count() == 0
    # Never silently two transactions.
    assert "one item" in body["message"]
    # The catalogue was never even ranked.
    assert fake_llm.ranking_calls == []


def test_two_distinct_items_are_multi_item_even_if_the_flag_says_otherwise(
    fake_llm, agent_catalogue, agent_purchase, db_session
):
    """Under-reporting the flag is the dangerous direction, so the server
    re-derives it from the item list."""
    fake_llm.set_intent(
        item="keyboard",
        quantity=1,
        max_budget=5000,
        is_multi_item=False,
        requested_items=["keyboard", "mouse"],
    )

    body = agent_purchase("keyboard and mouse under 5000")

    assert body["selection_action"] == "UNSUPPORTED"
    assert db_session.query(quote_db).count() == 0


def test_several_units_of_one_product_is_not_multi_item(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="wireless keyboard",
        quantity=3,
        max_budget=5000,
        hard_constraints=["wireless"],
        requested_items=["wireless keyboard"],
    )
    fake_llm.select_product(agent_catalogue["cheap_id"])

    body = agent_purchase("three wireless keyboards under 5000 total")

    assert body["selection_action"] == "PROPOSE"
    assert body["quote"]["quantity"] == 3


# --- nothing eligible ------------------------------------------------------


def test_no_eligible_candidate_is_a_no_match_not_a_quote(
    fake_llm, agent_catalogue, agent_purchase, db_session
):
    fake_llm.set_intent(
        item="wireless keyboard", quantity=1, max_budget=10,
        hard_constraints=["wireless"],
    )

    body = agent_purchase("wireless keyboard under 10 rupees")

    assert body["selection_action"] == "NO_MATCH"
    assert body["next_action"] == "stop"
    assert body["quote"] is None
    assert db_session.query(quote_db).count() == 0
