"""Package 4 -- natural language into a structured intent.

Covers what the AI is allowed to author (a reading of the request) and
what happens when it authors nonsense or is not configured at all.
"""

import pytest

from backend.agents.intent_parser import intent_from_payload, parse_intent
from backend.agents.llm_client import (
    ENV_API_KEY,
    ENV_MODEL,
    LLMConfigurationError,
    LLMResponseError,
    load_llm_config,
    parse_json_object,
)
from backend.databases.quote_db import quote_db

BUYER = "00000000-0000-0000-0000-000000000001"


# --- 1. a normal request becomes a structured intent -----------------------


def test_natural_language_becomes_a_structured_intent(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="wireless keyboard",
        quantity=1,
        max_budget=3000,
        hard_constraints=["wireless"],
        preferred_brand="Logitech",
        soft_preferences=["logitech"],
    )
    fake_llm.select_product(agent_catalogue["cheap_id"])

    body = agent_purchase("Get me a wireless keyboard under 3000, preferably Logitech")

    intent = body["intent"]
    assert intent["item"] == "wireless keyboard"
    assert intent["raw_text"] == (
        "Get me a wireless keyboard under 3000, preferably Logitech"
    )
    # The model was asked exactly once to read the sentence.
    assert len(fake_llm.intent_calls) == 1


# --- 2. an explicit quantity survives to the quote -------------------------


def test_explicit_quantity_is_preserved(fake_llm, agent_catalogue, agent_purchase):
    fake_llm.set_intent(
        item="wireless keyboard",
        quantity=3,
        max_budget=5000,
        hard_constraints=["wireless"],
    )
    fake_llm.select_product(agent_catalogue["cheap_id"])

    body = agent_purchase("I need three wireless keyboards, budget 5000 total")

    assert body["intent"]["quantity"] == 3
    assert body["quote"]["quantity"] == 3
    # Total is the server's arithmetic: unit price x quantity.
    assert float(body["quote"]["quoted_total"]) == pytest.approx(900.0 * 3)


# --- 3. an explicit budget is captured -------------------------------------


def test_explicit_max_budget_is_captured(fake_llm, agent_catalogue, agent_purchase):
    fake_llm.set_intent(item="wireless keyboard", max_budget=3000)
    fake_llm.select_product(agent_catalogue["mid_id"])

    body = agent_purchase("wireless keyboard under 3000")

    assert body["intent"]["max_budget"] == 3000.0


# --- 4. hard constraints are kept distinct from soft preferences -----------


def test_hard_constraints_are_distinct_from_soft_preferences(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="wireless keyboard",
        max_budget=3000,
        required_brand="Logitech",
        hard_constraints=["wireless"],
        preferred_brand="Logitech",
        soft_preferences=["compact", "quiet"],
    )
    fake_llm.select_product(agent_catalogue["cheap_id"])

    intent = agent_purchase("Logitech only, wireless, under 3000, ideally compact")[
        "intent"
    ]

    assert intent["required_brand"] == "Logitech"
    assert intent["hard_constraints"] == ["wireless"]
    assert intent["soft_preferences"] == ["compact", "quiet"]
    # The two lists are reported separately because they do different jobs:
    # one filters, one only ranks.
    assert set(intent["hard_constraints"]).isdisjoint(intent["soft_preferences"])


def test_a_required_brand_filters_and_a_preferred_brand_does_not(
    fake_llm, agent_catalogue, agent_purchase
):
    fake_llm.set_intent(
        item="wireless keyboard", max_budget=3000, required_brand="Dell"
    )
    agent_purchase("Dell only wireless keyboard under 3000")

    brands = {candidate["brand"] for candidate in fake_llm.candidates_shown()}
    assert brands == {"Dell"}


# --- 5. malformed model output fails safely --------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"item": "keyboard", "quantity": 0},
        {"item": "keyboard", "quantity": "three"},
        {"item": "keyboard", "max_budget": -5},
        {"item": "keyboard", "max_budget": "cheap"},
        {"item": "keyboard", "hard_constraints": "wireless"},
        {"item": "keyboard", "hard_constraints": [{"must": "be wireless"}]},
        {"item": 42},
        {"item": "keyboard", "needs_clarification": "yes"},
    ],
)
def test_malformed_intent_output_is_refused(
    payload, fake_llm, agent_catalogue, agent_purchase, db_session
):
    fake_llm.intent = payload

    body = agent_purchase("something", expect=502)

    assert body["detail"]["reason"] == "AGENT_RESPONSE_INVALID"
    # Nothing was priced on the way to failing.
    assert db_session.query(quote_db).count() == 0


def test_malformed_ranking_output_is_refused(
    fake_llm, agent_catalogue, agent_purchase, db_session
):
    fake_llm.set_intent(item="wireless keyboard", max_budget=3000)
    fake_llm.set_ranking(action="BUY_IT_NOW", product_id=agent_catalogue["cheap_id"])

    body = agent_purchase("wireless keyboard under 3000", expect=502)

    assert body["detail"]["reason"] == "AGENT_RESPONSE_INVALID"
    assert db_session.query(quote_db).count() == 0


def test_non_json_model_output_is_an_error_not_a_guess():
    with pytest.raises(LLMResponseError):
        parse_json_object("I think you want a keyboard!")
    with pytest.raises(LLMResponseError):
        parse_json_object("[1, 2, 3]")


def test_intent_payload_validation_is_explicit():
    with pytest.raises(LLMResponseError):
        intent_from_payload(
            {"item": "keyboard", "quantity": 1.5}, buyer_id=BUYER, raw_text="x"
        )


# --- 6. missing configuration is a controlled, explanatory failure ---------


def test_missing_api_key_names_the_variable(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.setenv(ENV_MODEL, "some-model")

    with pytest.raises(LLMConfigurationError) as exc:
        load_llm_config()
    assert ENV_API_KEY in str(exc.value)


def test_missing_model_names_the_variable(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "sk-not-a-real-key")
    monkeypatch.delenv(ENV_MODEL, raising=False)

    with pytest.raises(LLMConfigurationError) as exc:
        load_llm_config()
    assert ENV_MODEL in str(exc.value)


def test_blank_configuration_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "   ")
    monkeypatch.setenv(ENV_MODEL, "")

    with pytest.raises(LLMConfigurationError):
        load_llm_config()


def test_unconfigured_agent_endpoint_refuses_without_quoting(
    client, seed, agent_catalogue, db_session
):
    """The route surfaces the configuration error rather than pretending an
    AI ran, and nothing is priced."""
    from backend.agents.llm_client import get_llm
    from backend.main import app

    def _unconfigured(**kwargs):
        raise LLMConfigurationError("OPENAI_API_KEY is not set.")

    app.dependency_overrides[get_llm] = lambda: _unconfigured

    response = client.post(
        "/app/v1/agent/purchase",
        json={"buyer_id": seed["buyer_id"], "message": "a wireless keyboard"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "AGENT_NOT_CONFIGURED"
    assert db_session.query(quote_db).count() == 0


# --- buyer identity --------------------------------------------------------


def test_unknown_buyer_is_refused_before_any_model_call(
    fake_llm, agent_catalogue, agent_purchase
):
    body = agent_purchase(
        "a wireless keyboard",
        buyer_id="00000000-0000-0000-0000-0000000000ff",
        expect=404,
    )

    assert body["detail"]["reason"] == "BUYER_NOT_FOUND"
    assert fake_llm.calls == []


def test_the_request_schema_has_no_financial_fields():
    """Swagger-visible proof that a caller cannot supply money or policy."""
    from backend.models.agent_models import AgentPurchaseRequest

    assert set(AgentPurchaseRequest.model_fields) == {"buyer_id", "message"}


def test_empty_message_is_rejected_by_the_schema(client, seed):
    response = client.post(
        "/app/v1/agent/purchase", json={"buyer_id": seed["buyer_id"], "message": ""}
    )
    assert response.status_code == 422


def test_parse_intent_refuses_a_blank_message():
    with pytest.raises(LLMResponseError):
        parse_intent(llm=lambda **kw: {}, buyer_id=BUYER, message="   ")
