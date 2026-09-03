"""Deterministic stand-ins for everything outside this process.

The evaluation suite must produce the same numbers on every machine, on
every run, with no account, no key and no socket. That means three things
are faked, and all three are faked locally:

  * Razorpay order creation -- a recorder that hands back a fixed id.
  * Razorpay signature verification -- the real HMAC-SHA256 construction,
    computed here against a test secret. Signature handling is genuinely
    exercised; it just never leaves the process.
  * The language model -- a scripted callable. A scenario says what the
    model returns, so "the model said X, and the server did Y" is a
    controlled experiment rather than an observation.

`install_network_guard` then makes the absence of network traffic
structural rather than a promise: any code path that tries to reach a real
provider raises instead of quietly succeeding against a live account.
"""

import hashlib
import hmac
import importlib
import json

TEST_KEY_ID = "rzp_test_evaluation_key_id"
TEST_KEY_SECRET = "evaluation_only_secret"

FAKE_ORDER_ID = "order_EVAL123"
FAKE_PAYMENT_ID = "pay_EVAL123"


def sign(order_id, payment_id, secret=TEST_KEY_SECRET):
    """The signature Razorpay Checkout would hand back for this payment."""
    return hmac.new(
        secret.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class FakeSignatureError(Exception):
    """Mirrors razorpay.errors.SignatureVerificationError."""


class ProviderDown(Exception):
    """What a Razorpay outage looks like from inside `order.create`."""


class FakeOrders:
    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, payload):
        self._recorder.append(payload)
        return {"id": FAKE_ORDER_ID}


class FailingOrders:
    """Order creation that always throws.

    Records the attempt first, so a scenario can prove the call was made
    and still failed -- as opposed to never being reached at all, which is
    what a policy BLOCK looks like.
    """

    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, payload):
        self._recorder.append(payload)
        raise ProviderDown("Razorpay is unreachable")


class FakeUtility:
    """Same contract as razorpay.utility.Utility.verify_payment_signature:
    HMAC-SHA256 of "order_id|payment_id" keyed by the API secret, raising
    on mismatch. Purely local -- it computes, it does not call out."""

    def __init__(self, secret, recorder):
        self._secret = secret
        self._recorder = recorder

    def verify_payment_signature(self, parameters):
        self._recorder.append(dict(parameters))
        expected = sign(
            parameters["razorpay_order_id"],
            parameters["razorpay_payment_id"],
            self._secret,
        )
        if not hmac.compare_digest(expected, str(parameters["razorpay_signature"])):
            raise FakeSignatureError("Razorpay Signature Verification Failed")
        return True


class FakeRazorpayModule:
    """Stands in for the `razorpay` module inside product_routes.

    `orders_created` and `signatures_checked` are the two observations
    almost every payment scenario makes: whether the provider was reached
    at all, and whether a signature was ever put to it.
    """

    def __init__(self, secret=TEST_KEY_SECRET, working=True):
        self.secret = secret
        self.working = working
        self.orders_created = []
        self.signatures_checked = []

    def Client(self, auth=None):  # noqa: N802 - mirrors razorpay.Client
        orders = (
            FakeOrders(self.orders_created)
            if self.working
            else FailingOrders(self.orders_created)
        )
        client = type("FakeClient", (), {})()
        client.order = orders
        client.utility = FakeUtility(self.secret, self.signatures_checked)
        return client


# ---------------------------------------------------------------------------
# The language model
# ---------------------------------------------------------------------------

# Markers taken from the two system prompts, so the fake can tell which
# question it is being asked without a scenario having to care.
_INTENT_MARKER = "intent parser"
_RANKER_MARKER = "product-selection assistant"


class FakeLLM:
    """A scripted stand-in for `llm_client.complete_json`.

    Returns exactly what a scenario told it to return, and records every
    prompt so a scenario can assert on what the model was actually shown.
    """

    def __init__(self):
        self.intent = {"item": "widget", "quantity": 1}
        # None means "nominate the first eligible candidate".
        self.ranking = None
        self.calls = []
        self.raise_on_intent = None

    # --- scripting -------------------------------------------------------

    def set_intent(self, **fields):
        self.intent = fields
        return self

    def set_ranking(self, **fields):
        self.ranking = fields
        return self

    def select_product(self, product_id, rationale="scripted rationale"):
        return self.set_ranking(
            action="SELECT", product_id=str(product_id), rationale=rationale
        )

    def ask_to_clarify(self, question="Which one?"):
        return self.set_ranking(
            action="CLARIFY", product_id=None, clarification_question=question
        )

    # --- inspection ------------------------------------------------------

    @property
    def ranking_calls(self):
        return [call for call in self.calls if _RANKER_MARKER in call["system"]]

    def candidates_shown(self):
        """The candidate list the ranker was actually given."""
        if not self.ranking_calls:
            return []
        return candidates_in(self.ranking_calls[-1]["user"])

    # --- the callable itself ---------------------------------------------

    def __call__(self, *, system, user):
        self.calls.append({"system": system, "user": user})

        if _INTENT_MARKER in system:
            return dict(self.intent)

        if self.ranking is not None:
            return dict(self.ranking)

        candidates = candidates_in(user)
        if not candidates:
            return {
                "action": "CLARIFY",
                "product_id": None,
                "clarification_question": "Nothing to choose from.",
            }
        return {
            "action": "SELECT",
            "product_id": candidates[0]["product_id"],
            "rationale": "first eligible candidate",
        }


def candidates_in(user_prompt):
    """Pull the JSON candidate array back out of the ranking prompt."""
    for line in user_prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            return json.loads(stripped)
    return []


# ---------------------------------------------------------------------------
# Network guard
# ---------------------------------------------------------------------------


class NetworkAccessError(AssertionError):
    """The evaluation tried to reach the outside world."""


def install_network_guard():
    """Sever every transport a real provider call would use.

    Returns the list of things that were patched, so the runner can state
    plainly what it cut rather than merely claiming it cut something.

    Blocking is applied at the httpx TRANSPORT layer rather than on the
    client, because the FastAPI test client is itself an httpx client --
    severing it would break the harness instead of the network. Only the
    transports that open a real socket are cut.
    """
    patched = []

    import requests

    def _blocked(*args, **kwargs):
        raise NetworkAccessError(
            "The evaluation harness must not make outbound calls. Razorpay "
            "and the language model are faked."
        )

    requests.sessions.Session.request = _blocked
    requests.sessions.Session.send = _blocked
    patched.append("requests.Session.request/send")

    for module_name in ("httpx", "httpx2"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, "HTTPTransport"):
            module.HTTPTransport.handle_request = _blocked
            patched.append(f"{module_name}.HTTPTransport.handle_request")
        if hasattr(module, "AsyncHTTPTransport"):
            module.AsyncHTTPTransport.handle_async_request = _blocked
            patched.append(f"{module_name}.AsyncHTTPTransport.handle_async_request")

    # A second, independent guarantee: a code path that forgets to inject a
    # fake model reaches this instead of OpenAI.
    from backend.agents import llm_client

    llm_client.complete_json = _blocked
    patched.append("llm_client.complete_json")

    return patched
