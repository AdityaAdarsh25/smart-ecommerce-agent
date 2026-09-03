"""Every source module must import, and the app must still build."""

import importlib
import pkgutil

import pytest

MODULES = [
    "backend.database",
    "backend.money",
    "backend.main",
    "backend.commerce.product_routes",
    "backend.commerce.approval_routes",
    "backend.policies.policy_engine",
    "backend.enums.ApprovalStatus",
    "backend.enums.PolicyDecision",
    "backend.enums.SelectionAction",
    "backend.enums.TransactionStatus",
    "backend.enums.QuoteStatus",
    "backend.enums.RejectionReason",
    "backend.models.Approval",
    "backend.models.Buyer",
    "backend.models.Intent",
    "backend.models.Mandate",
    "backend.models.Merchant",
    "backend.models.PolicyResult",
    "backend.models.Product",
    "backend.models.Quote",
    "backend.models.RankedCandidates",
    "backend.models.SelectionDecision",
    "backend.models.Transaction",
    "backend.models.order_request",
    "backend.models.create_order_request",
    "backend.models.payment_request",
    "backend.models.responses",
    "backend.databases.approval_db",
    "backend.databases.buyer_db",
    "backend.databases.mandate_db",
    "backend.databases.merchant_db",
    "backend.databases.product_db",
    "backend.databases.quote_db",
    "backend.databases.transaction_db",
    "backend.agents.selection_decision",
    "backend.agents.buyer_agent",
    "backend.agents.catalogue",
    "backend.agents.intent_parser",
    "backend.agents.llm_client",
    "backend.agents.product_ranker",
    "backend.commerce.agent_routes",
    "backend.enums.NextAction",
    "backend.models.agent_models",
    "backend.commerce.demo_routes",
]


# --- 15. all source modules import successfully ----------------------------


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)


def test_selection_decision_is_usable(client, seed):
    """It previously failed to import because RankedCandidates declared
    `score:0.0` -- a literal as a type annotation."""
    from backend.agents.selection_decision import decide_product_selection
    from backend.enums.SelectionAction import SelectionAction
    from backend.models.Intent import Intent

    products = client.get("/app/v1/search", params={"item": "Test"}).json()
    intent = Intent(
        buyer_id="00000000-0000-0000-0000-000000000001",
        raw_text="get me a test widget under 150",
        item="Test Widget",
        max_budget=150.0,
        quantity=1,
    )

    decision = decide_product_selection(intent, products)
    assert decision.action in set(SelectionAction)
    # Product selection proposes; it never authorizes payment.
    assert not hasattr(decision, "amount")
    assert not hasattr(decision, "status")


def test_ranked_candidate_score_is_a_float():
    from backend.models.RankedCandidates import RankedCandidate
    from backend.models.Product import Product

    candidate = RankedCandidate(
        product=Product(
            product_name="x",
            brand="y",
            cost=1.0,
            quantity=1,
            merchant_id="00000000-0000-0000-0000-000000000001",
        )
    )
    assert candidate.score == 0.0
    assert isinstance(candidate.score, float)


def test_no_module_still_references_the_removed_search_helper():
    import backend.agents

    names = {m.name for m in pkgutil.iter_modules(backend.agents.__path__)}
    assert "search_helper" not in names


def test_the_agent_route_is_registered_with_a_typed_response():
    """Swagger must document a real schema, not a bare string."""
    from backend.main import app

    spec = app.openapi()
    operation = spec["paths"]["/app/v1/agent/purchase"]["post"]

    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]
    response_ref = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert request_ref["$ref"].endswith("AgentPurchaseRequest")
    assert response_ref["$ref"].endswith("AgentPurchaseResponse")

    schema = spec["components"]["schemas"]["AgentPurchaseResponse"]
    assert set(schema["properties"]) >= {
        "selection_action",
        "next_action",
        "quote",
        "policy",
        "intent",
        "selection",
    }


def test_next_action_is_derived_not_chosen():
    """No input to this function is anything a model wrote."""
    from backend.enums.NextAction import NextAction, next_action_for
    from backend.enums.PolicyDecision import PolicyDecision
    from backend.enums.SelectionAction import SelectionAction

    propose = SelectionAction.PROPOSE
    assert next_action_for(propose, PolicyDecision.ALLOW) is NextAction.CREATE_ORDER
    assert (
        next_action_for(propose, PolicyDecision.REQUIRE_APPROVAL)
        is NextAction.AWAIT_APPROVAL
    )
    assert next_action_for(propose, PolicyDecision.BLOCK) is NextAction.STOP
    assert next_action_for(SelectionAction.CLARIFY, None) is NextAction.CLARIFY
    assert next_action_for(SelectionAction.NO_MATCH, None) is NextAction.STOP
    assert next_action_for(SelectionAction.UNSUPPORTED, None) is NextAction.STOP


def test_the_policy_engine_never_imports_the_llm_client():
    """The financial decision must not be reachable from the model layer."""
    from pathlib import Path

    source = Path("backend/policies/policy_engine.py").read_text(encoding="utf-8")
    assert "llm" not in source.lower().replace("no llm output", "")
