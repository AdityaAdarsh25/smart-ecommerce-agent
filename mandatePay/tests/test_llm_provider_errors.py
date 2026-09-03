"""What happens when the provider itself fails, rather than the model.

Two upstream failures are covered here, and the distinction between them
is the point:

  * a 429 is the provider throttling us. Nothing was wrong with the
    request, no reply exists to validate, and asking again immediately
    would only deepen the limit -- so it is surfaced at once as a
    controlled refusal that says "retry shortly".

  * a reply that is not usable JSON -- whether the provider returned the
    text or rejected its own generation with a 400 -- is a compatibility
    problem that another attempt can genuinely resolve, so it is retried
    a bounded number of times and then refused.

Both refusals share the property that matters: no quote, no transaction,
no approval, no provider call, and no change to a balance or to stock.
"""

import httpx
import pytest
from openai import BadRequestError, RateLimitError

from backend.agents.llm_client import (
    MAX_JSON_ATTEMPTS,
    LLMConfig,
    LLMRateLimitError,
    LLMResponseError,
    complete_json,
)
from backend.databases.approval_db import approval_db
from backend.databases.buyer_db import buyer_db
from backend.databases.product_db import product_db
from backend.databases.quote_db import quote_db
from backend.databases.transaction_db import transaction_db

CONFIG = LLMConfig(api_key="test-key-not-real", model="test-model")

INTENT_JSON = '{"item": "wireless keyboard", "quantity": 1, "max_budget": 3000}'


# --- building upstream failures the SDK would actually raise ---------------


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers or {},
        request=httpx.Request("POST", "https://provider.invalid/v1/chat/completions"),
    )


def rate_limited(retry_after: str | None = None) -> RateLimitError:
    """The 429 an OpenAI-compatible provider raises through the SDK."""
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return RateLimitError(
        "Rate limit reached for model on tokens per minute (TPM).",
        response=_response(429, headers),
        body=None,
    )


def json_validation_failed() -> BadRequestError:
    """A provider rejecting its own model's reply as invalid JSON."""
    return BadRequestError(
        "Failed to validate JSON. Please adjust your prompt.",
        response=_response(400),
        body={"error": {"code": "json_validate_failed"}},
    )


class StubCompletions:
    """A scripted provider. Each script entry is a reply or a failure."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_request = None
        # Every `with_options(...)` the code made before calling, so a test
        # can assert how the request was configured.
        self.request_options = []

    def create(self, **kwargs):
        self.calls += 1
        self.last_request = kwargs
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        message = type("Message", (), {"content": item})()
        choice = type("Choice", (), {"message": message})()
        return type("Completion", (), {"choices": [choice]})()


@pytest.fixture()
def provider(monkeypatch):
    """Install a scripted provider behind the real `complete_json`.

    The SDK client is replaced, not the project's code, so everything
    under test -- the retry bound, the error mapping, the header read --
    is the production path.
    """

    def _install(*script):
        completions = StubCompletions(script)

        class Client:
            chat = type("Chat", (), {"completions": completions})()

            def with_options(self, **options):
                completions.request_options.append(options)
                # The real SDK returns a configured copy; the stub shares
                # the same scripted completions so calls stay counted.
                return self

        monkeypatch.setattr("openai.OpenAI", lambda **kwargs: Client())
        return completions

    return _install


# --- 1. the request itself disables the SDK's own retrying -----------------


def test_the_completion_request_disables_sdk_retries(provider):
    """A 429 must not be retried underneath us: on a metered tier that
    spends quota on a request already known to be throttled."""
    completions = provider(INTENT_JSON)

    complete_json(system="s", user="u", config=CONFIG)

    assert completions.request_options == [{"max_retries": 0}]


def test_the_installed_sdk_honours_that_option():
    """The stub above asserts we ask; this asserts the real SDK listens,
    so the two cannot drift apart silently."""
    from openai import OpenAI

    client = OpenAI(api_key="test-key-not-real", base_url="https://provider.invalid/v1")

    assert client.max_retries > 0  # the default we are overriding
    assert client.with_options(max_retries=0).max_retries == 0


# --- 2. a rate limit is a controlled refusal, not a 500 --------------------


def test_upstream_rate_limit_becomes_a_rate_limit_error(provider):
    completions = provider(rate_limited())

    with pytest.raises(LLMRateLimitError):
        complete_json(system="s", user="u", config=CONFIG)

    # Asked once. A limiter is not something to retry into.
    assert completions.calls == 1


def test_rate_limit_is_not_reported_as_a_malformed_reply(provider):
    """The two failures stay distinguishable: one is retryable by the
    caller shortly, the other means the model cannot be read at all."""
    provider(rate_limited())

    with pytest.raises(LLMRateLimitError) as exc:
        complete_json(system="s", user="u", config=CONFIG)

    assert not isinstance(exc.value, LLMResponseError)


def test_rate_limited_endpoint_refuses_without_quoting(
    client, seed, agent_catalogue, db_session
):
    from backend.agents.llm_client import get_llm
    from backend.main import app

    def _rate_limited(**kwargs):
        raise LLMRateLimitError("The AI provider is rate limited.")

    app.dependency_overrides[get_llm] = lambda: _rate_limited

    balance_before = db_session.get(buyer_db, seed["buyer_id"]).balance
    stock_before = {
        str(row.product_id): row.quantity for row in db_session.query(product_db)
    }

    response = client.post(
        "/app/v1/agent/purchase",
        json={"buyer_id": seed["buyer_id"], "message": "a wireless keyboard"},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason"] == "AGENT_RATE_LIMITED"
    # The caller is told what happened and that it may try again.
    assert "rate limited" in detail["message"].lower()
    assert "again" in detail["message"].lower()

    # Nothing financial moved on the way to failing.
    assert db_session.query(quote_db).count() == 0
    assert db_session.query(transaction_db).count() == 0
    assert db_session.query(approval_db).count() == 0

    db_session.expire_all()
    assert db_session.get(buyer_db, seed["buyer_id"]).balance == balance_before
    assert {
        str(row.product_id): row.quantity for row in db_session.query(product_db)
    } == stock_before


def test_rate_limit_is_recorded_as_a_failed_agent_request(
    client, seed, agent_catalogue, event_types
):
    from backend.agents.llm_client import get_llm
    from backend.main import app

    def _rate_limited(**kwargs):
        raise LLMRateLimitError("The AI provider is rate limited.")

    app.dependency_overrides[get_llm] = lambda: _rate_limited

    client.post(
        "/app/v1/agent/purchase",
        json={"buyer_id": seed["buyer_id"], "message": "a wireless keyboard"},
    )

    assert "AGENT_REQUEST_FAILED" in event_types()


# --- 3. Retry-After is optional and never trusted blindly ------------------


def test_retry_after_is_surfaced_when_the_provider_sends_one(provider):
    provider(rate_limited(retry_after="9.36"))

    with pytest.raises(LLMRateLimitError) as exc:
        complete_json(system="s", user="u", config=CONFIG)

    assert exc.value.retry_after_seconds == pytest.approx(9.36)


@pytest.mark.parametrize(
    "header",
    [
        None,  # the common case: no hint at all
        "soon",  # not a number
        "",  # present but empty
        "-5",  # nonsensical
        "999999",  # implausible, so reported as no hint
    ],
)
def test_absent_or_unusable_retry_after_is_simply_absent(provider, header):
    provider(rate_limited(retry_after=header))

    with pytest.raises(LLMRateLimitError) as exc:
        complete_json(system="s", user="u", config=CONFIG)

    assert exc.value.retry_after_seconds is None


def test_retry_after_field_is_optional_in_the_response_body(client, seed, agent_catalogue):
    """A refusal without a hint still validates and still says nothing
    about when to retry, rather than inventing a number."""
    from backend.agents.llm_client import get_llm
    from backend.main import app

    def _rate_limited(**kwargs):
        raise LLMRateLimitError("The AI provider is rate limited.")

    app.dependency_overrides[get_llm] = lambda: _rate_limited

    response = client.post(
        "/app/v1/agent/purchase",
        json={"buyer_id": seed["buyer_id"], "message": "a wireless keyboard"},
    )

    assert response.json()["detail"]["retry_after_seconds"] is None


def test_a_provider_hint_reaches_the_caller(client, seed, agent_catalogue):
    from backend.agents.llm_client import get_llm
    from backend.main import app

    def _rate_limited(**kwargs):
        raise LLMRateLimitError("Rate limited.", retry_after_seconds=12.0)

    app.dependency_overrides[get_llm] = lambda: _rate_limited

    response = client.post(
        "/app/v1/agent/purchase",
        json={"buyer_id": seed["buyer_id"], "message": "a wireless keyboard"},
    )

    assert response.json()["detail"]["retry_after_seconds"] == 12.0


# --- 4. the JSON retry still behaves as accepted ---------------------------


def test_unusable_json_is_retried_then_succeeds(provider):
    completions = provider("not json at all", INTENT_JSON)

    parsed = complete_json(system="s", user="u", config=CONFIG)

    assert parsed["item"] == "wireless keyboard"
    assert completions.calls == 2


def test_provider_side_json_validation_failure_is_retried(provider):
    completions = provider(json_validation_failed(), INTENT_JSON)

    parsed = complete_json(system="s", user="u", config=CONFIG)

    assert parsed["item"] == "wireless keyboard"
    assert completions.calls == 2


def test_persistently_unusable_json_is_refused_after_a_bounded_number_of_tries(
    provider,
):
    completions = provider(*(["still not json"] * MAX_JSON_ATTEMPTS))

    with pytest.raises(LLMResponseError):
        complete_json(system="s", user="u", config=CONFIG)

    assert completions.calls == MAX_JSON_ATTEMPTS


def test_the_json_retry_does_not_swallow_a_rate_limit(provider):
    """A 429 arriving mid-retry ends the attempt immediately -- it is not
    absorbed as one more unusable reply."""
    completions = provider("not json at all", rate_limited(), INTENT_JSON)

    with pytest.raises(LLMRateLimitError):
        complete_json(system="s", user="u", config=CONFIG)

    assert completions.calls == 2


def test_an_unrelated_bad_request_is_not_retried(provider):
    """A 400 that is not about JSON is a real request problem, so it
    propagates rather than being tried twice more."""
    completions = provider(
        BadRequestError(
            "The model `nope` does not exist.",
            response=_response(400),
            body={"error": {"code": "model_not_found"}},
        )
    )

    with pytest.raises(BadRequestError):
        complete_json(system="s", user="u", config=CONFIG)

    assert completions.calls == 1


# --- 5. the ordinary path is untouched -------------------------------------


def test_a_good_reply_is_returned_on_the_first_attempt(provider):
    completions = provider(INTENT_JSON)

    parsed = complete_json(system="s", user="u", config=CONFIG)

    assert parsed == {"item": "wireless keyboard", "quantity": 1, "max_budget": 3000}
    assert completions.calls == 1


def test_the_request_sent_to_the_provider_is_unchanged(provider):
    """JSON mode, temperature 0, and the two messages the agent supplies."""
    completions = provider(INTENT_JSON)

    complete_json(system="a system prompt", user="a user prompt", config=CONFIG)

    sent = completions.last_request
    assert sent["model"] == "test-model"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["temperature"] == 0
    assert sent["messages"] == [
        {"role": "system", "content": "a system prompt"},
        {"role": "user", "content": "a user prompt"},
    ]
