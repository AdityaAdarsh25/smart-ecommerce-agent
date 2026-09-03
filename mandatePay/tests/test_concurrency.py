"""Two money actions arriving at once.

Every financial check in MandatePay is read-then-write: read the world,
decide, write the decision. Between the read and the write, a second
request can read the same world and reach the same decision, because a
decision that has not been committed yet is invisible to it. That is not
a hypothetical -- it is how two concurrent `/create-order` calls both
cleared duplicate detection, and how two concurrent `/payment/verify`
calls could both read the same monthly spend and both settle.

The fix is one shared in-process critical section
(`backend.commerce.serialization`), and these tests are what hold it in
place.

HOW THE RACE IS MADE REAL

Threads are started on a barrier so they enter the endpoint together, and
a delay is injected INSIDE the section under test -- into the duplicate
check, or into the monthly-spend read. The delay is what forces the
interleaving: without the lock, the second thread walks straight through
the check the first one is still standing in, and these tests fail.

Unlike the rest of the suite these run against a temporary FILE database,
because each thread needs its own connection -- the in-memory StaticPool
everything else uses hands out one shared connection and would serialize
the very thing being measured.

SCOPE

This is the supported v1 runtime: one FastAPI/Uvicorn process. The lock
is a `threading` primitive and protects nothing across processes. A
multi-worker deployment would need database-backed serialization instead.
"""

import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.commerce import approval_routes, product_routes
from backend.commerce.approval_routes import approve_transaction, reject_transaction
from backend.commerce.product_routes import create_order, quote_and_evaluate, verify_payment
from backend.database import Base
from backend.databases.approval_db import approval_db
from backend.databases.buyer_db import buyer_db
from backend.databases.mandate_db import mandate_db
from backend.databases.merchant_db import merchant_db
from backend.databases.product_db import product_db
from backend.databases.quote_db import quote_db
from backend.databases.transaction_db import transaction_db
from backend.enums.ApprovalStatus import ApprovalStatus
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import TransactionStatus
from backend.models.create_order_request import CreateOrderRequest
from backend.models.payment_request import PaymentVerificationRequest
from tests.conftest import (
    BALANCE,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    FakeUtility,
    sign,
)

WIDGET_COST = 100.0
GADGET_COST = 250.0
STOCK = 100

# Long enough that the second thread is provably inside the endpoint and
# waiting while the first one is mid-decision; short enough to be free.
INTERLEAVE_DELAY = 0.15


# ---------------------------------------------------------------------------
# A provider that hands out a distinct order id per call
# ---------------------------------------------------------------------------


class CountingOrders:
    """`order.create`, recording every call. The id is unique per order so
    two concurrent orders cannot be mistaken for one."""

    def __init__(self, recorder, lock):
        self._recorder = recorder
        self._lock = lock

    def create(self, payload):
        with self._lock:
            self._recorder.append(payload)
            number = len(self._recorder)
        return {"id": f"order_FAKE{number}"}


class CountingRazorpay:
    """Stands in for the `razorpay` module. Purely local: it computes an
    HMAC and appends to a list, and it never opens a socket."""

    def __init__(self, secret=TEST_KEY_SECRET):
        self.secret = secret
        self.orders_created = []
        self.signatures_checked = []
        self._lock = threading.Lock()

    def Client(self, auth=None):  # noqa: N802 - mirrors razorpay.Client
        client = type("FakeClient", (), {})()
        client.order = CountingOrders(self.orders_created, self._lock)
        client.utility = FakeUtility(self.secret, self.signatures_checked)
        return client


# ---------------------------------------------------------------------------
# A threadable environment
# ---------------------------------------------------------------------------


class Env:
    """One database, several sessions -- one per simulated request.

    Requests are made by calling the endpoint functions directly rather
    than through TestClient: the endpoints take their session as an
    argument, and driving them this way gives each thread a genuinely
    separate session and connection, which is the shape the race needs.
    """

    def __init__(self, sessionmaker_, ids, razorpay):
        self._sessionmaker = sessionmaker_
        self.ids = ids
        self.razorpay = razorpay
        self.sessions = []

    def session(self):
        session = self._sessionmaker()
        self.sessions.append(session)
        return session

    def quote(self, *, product="widget_id", quantity):
        session = self.session()
        try:
            body = quote_and_evaluate(
                session,
                buyer_id=self.ids["buyer_id"],
                product_id=self.ids[product],
                quantity=quantity,
            )
            return str(body.quote_id)
        finally:
            session.close()

    def order(self, quote_id):
        """One create-order call, on its own session. Returns the response
        or the refusal, never raises."""
        session = self.session()
        try:
            return create_order(CreateOrderRequest(quote_id=quote_id), db=session)
        except HTTPException as exc:
            return exc
        finally:
            session.close()

    def settle(self, transaction_id, order_id, payment_id):
        session = self.session()
        try:
            return verify_payment(
                PaymentVerificationRequest(
                    transaction_id=transaction_id,
                    razorpay_order_id=order_id,
                    razorpay_payment_id=payment_id,
                    razorpay_signature=sign(order_id, payment_id),
                ),
                db=session,
            )
        except HTTPException as exc:
            return exc
        finally:
            session.close()

    def approve(self, transaction_id):
        session = self.session()
        try:
            return approve_transaction(transaction_id, None, db=session)
        except HTTPException as exc:
            return exc
        finally:
            session.close()

    def reject(self, transaction_id):
        session = self.session()
        try:
            return reject_transaction(transaction_id, None, db=session)
        except HTTPException as exc:
            return exc
        finally:
            session.close()

    def set_limits(self, **fields):
        session = self.session()
        try:
            mandate = (
                session.query(mandate_db)
                .filter(mandate_db.buyer_id == self.ids["buyer_id"])
                .first()
            )
            for name, value in fields.items():
                setattr(mandate, name, value)
            session.commit()
        finally:
            session.close()

    # --- observation -----------------------------------------------------

    def read(self):
        return self.session()

    def transactions(self):
        session = self.read()
        try:
            return session.query(transaction_db).all()
        finally:
            session.close()

    def balance(self):
        session = self.read()
        try:
            return session.get(buyer_db, self.ids["buyer_id"]).balance
        finally:
            session.close()

    def stock(self, product="widget_id"):
        session = self.read()
        try:
            return session.get(product_db, self.ids[product]).quantity
        finally:
            session.close()

    def quote_status(self, quote_id):
        session = self.read()
        try:
            return session.get(quote_db, str(quote_id)).status
        finally:
            session.close()

    def approvals(self, transaction_id):
        session = self.read()
        try:
            return (
                session.query(approval_db)
                .filter(approval_db.transaction_id == str(transaction_id))
                .all()
            )
        finally:
            session.close()


@pytest.fixture()
def env(monkeypatch):
    # A disposable directory of our own rather than pytest's `tmp_path`,
    # which needs a shared basetemp this suite has no claim on. Removed
    # again below, so nothing survives the test.
    workdir = tempfile.mkdtemp(prefix="mandatepay-concurrency-")
    path = os.path.join(workdir, "concurrency.db")
    engine = create_engine(
        f"sqlite:///{path}",
        # Sessions are handed between threads exactly as a web server
        # hands them out per request. `timeout` is SQLite's own busy wait,
        # so a writer never fails outright while another commits.
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    setup = Session()
    buyer = buyer_db(name="Concurrent Buyer", balance=BALANCE)
    setup.add(buyer)
    setup.flush()
    setup.add(
        mandate_db(
            buyer_id=buyer.id,
            autonomous_limit=5000.0,
            absolute_transaction_limit=50000.0,
            monthly_cap=5000.0,
        )
    )
    merchant = merchant_db(name="TestMart")
    setup.add(merchant)
    setup.flush()
    widget = product_db(
        product_name="Test Widget",
        brand="Acme",
        cost=WIDGET_COST,
        category="gadgets",
        quantity=STOCK,
        merchant_id=merchant.id,
    )
    gadget = product_db(
        product_name="Test Gadget",
        brand="Acme",
        cost=GADGET_COST,
        category="gadgets",
        quantity=STOCK,
        merchant_id=merchant.id,
    )
    setup.add_all([widget, gadget])
    setup.commit()
    ids = {
        "buyer_id": str(buyer.id),
        "widget_id": str(widget.product_id),
        "gadget_id": str(gadget.product_id),
    }
    setup.close()

    razorpay = CountingRazorpay()
    monkeypatch.setattr(product_routes, "razorpay", razorpay)
    monkeypatch.setattr(product_routes, "key_id", TEST_KEY_ID)
    monkeypatch.setattr(product_routes, "key_secret", TEST_KEY_SECRET)

    environment = Env(Session, ids, razorpay)
    yield environment

    for session in environment.sessions:
        session.close()
    engine.dispose()
    shutil.rmtree(workdir, ignore_errors=True)


def in_parallel(*calls):
    """Run the calls at the same instant and return their results in order.

    The barrier is what makes this a race rather than two requests that
    happen to be near each other: no call proceeds until every thread has
    arrived.
    """
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(run, call) for call in calls]
        return [future.result(timeout=30) for future in futures]


@pytest.fixture()
def slow_duplicate_check(monkeypatch):
    """Widen the window between the duplicate READ and the transaction
    WRITE that would have closed it.

    This is the race, made big enough to observe. The delay sits inside
    the critical section, so with the lock held a second caller waits;
    without it, the second caller reads "no duplicate" while the first is
    still standing in the same gap.
    """
    real = product_routes._has_active_duplicate

    def slow(*args, **kwargs):
        result = real(*args, **kwargs)
        time.sleep(INTERLEAVE_DELAY)
        return result

    monkeypatch.setattr(product_routes, "_has_active_duplicate", slow)


@pytest.fixture()
def slow_monthly_spend(monkeypatch):
    """The same widening, applied to the monthly-spend read that governs
    settlement."""
    real = product_routes._monthly_spend

    def slow(*args, **kwargs):
        result = real(*args, **kwargs)
        time.sleep(INTERLEAVE_DELAY)
        return result

    monkeypatch.setattr(product_routes, "_monthly_spend", slow)


def outcomes(results):
    """(successes, refusals) split out of a parallel run."""
    refused = [r for r in results if isinstance(r, HTTPException)]
    ok = [r for r in results if not isinstance(r, HTTPException)]
    return ok, refused


# ---------------------------------------------------------------------------
# D. Two create-order calls for the SAME quote
# ---------------------------------------------------------------------------


def test_same_quote_ordered_twice_at_once_creates_one_provider_order(
    env, slow_duplicate_check
):
    quote_id = env.quote(quantity=10)  # 1,000.00

    first, second = in_parallel(
        lambda: env.order(quote_id),
        lambda: env.order(quote_id),
    )

    responses, refusals = outcomes([first, second])
    assert not refusals, "neither call should error; one is simply blocked"

    statuses = sorted(r.transaction.status.value for r in responses)
    assert statuses == [
        TransactionStatus.BLOCKED.value,
        TransactionStatus.ORDER_CREATED.value,
    ]

    # Exactly one order reached the provider. A second one here would be a
    # real Razorpay order nothing can ever settle.
    assert len(env.razorpay.orders_created) == 1

    blocked = next(
        r for r in responses if r.transaction.status is TransactionStatus.BLOCKED
    )
    assert "duplicate_purchase" in blocked.policy.violation_codes
    assert blocked.razorpay is None

    live = [
        txn
        for txn in env.transactions()
        if txn.status is TransactionStatus.ORDER_CREATED
    ]
    assert len(live) == 1
    assert env.quote_status(quote_id) is QuoteStatus.ACTIVE  # nothing settled


# ---------------------------------------------------------------------------
# E. Two create-order calls for DISTINCT but identical quotes
# ---------------------------------------------------------------------------


def test_two_identical_quotes_ordered_at_once_are_deduplicated(
    env, slow_duplicate_check
):
    """Same buyer, product, quantity and amount, two separate ACTIVE
    quotes. Quote consumption cannot help here -- they are different
    quotes -- so duplicate protection has to hold on its own."""
    first_quote = env.quote(quantity=10)
    second_quote = env.quote(quantity=10)
    assert first_quote != second_quote

    results = in_parallel(
        lambda: env.order(first_quote),
        lambda: env.order(second_quote),
    )
    responses, refusals = outcomes(results)
    assert not refusals

    statuses = sorted(r.transaction.status.value for r in responses)
    assert statuses == [
        TransactionStatus.BLOCKED.value,
        TransactionStatus.ORDER_CREATED.value,
    ]

    assert len(env.razorpay.orders_created) == 1

    executable = [
        txn
        for txn in env.transactions()
        if txn.status in (TransactionStatus.ORDER_CREATED, TransactionStatus.AUTHORIZED)
    ]
    assert len(executable) == 1


# ---------------------------------------------------------------------------
# F. Two settlements at once against one monthly cap
# ---------------------------------------------------------------------------


def payable(env, *, product, quantity):
    """A transaction sitting at ORDER_CREATED, created serially."""
    quote_id = env.quote(product=product, quantity=quantity)
    response = env.order(quote_id)
    assert response.transaction.status is TransactionStatus.ORDER_CREATED
    return response.transaction


def test_two_settlements_at_once_cannot_race_the_monthly_cap(
    env, slow_monthly_spend
):
    """3,000 + 3,000 against a 5,000 cap, verified simultaneously.

    Both are individually within the cap and neither has settled, so both
    reads of "spend so far" see 0.00. Only serialization can stop the
    second one.
    """
    first = payable(env, product="widget_id", quantity=30)  # 3,000.00
    second = payable(env, product="gadget_id", quantity=12)  # 3,000.00

    results = in_parallel(
        lambda: env.settle(first.id, first.razorpay_order_id, "pay_ONE"),
        lambda: env.settle(second.id, second.razorpay_order_id, "pay_TWO"),
    )
    settled, refused = outcomes(results)

    assert len(settled) == 1, "two payments settled against one cap"
    assert len(refused) == 1
    assert (
        refused[0].detail["reason"] == RejectionReason.MONTHLY_CAP_EXCEEDED.value
    )

    paid = [txn for txn in env.transactions() if txn.status is TransactionStatus.PAID]
    assert len(paid) == 1
    assert paid[0].paid_at is not None

    # The money moved once, for the one that settled.
    assert env.balance() == BALANCE - 3000.0
    moved = (STOCK - env.stock("widget_id")) + (STOCK - env.stock("gadget_id"))
    assert moved in (30, 12)
    assert (STOCK - env.stock("widget_id")) * WIDGET_COST + (
        STOCK - env.stock("gadget_id")
    ) * GADGET_COST == 3000.0


def test_two_settlements_within_the_cap_both_succeed(env, slow_monthly_spend):
    """Serialization must not refuse what the mandate actually permits:
    2,000 + 2,500 fits inside 5,000, concurrently or not."""
    first = payable(env, product="widget_id", quantity=20)  # 2,000.00
    second = payable(env, product="gadget_id", quantity=10)  # 2,500.00

    results = in_parallel(
        lambda: env.settle(first.id, first.razorpay_order_id, "pay_ONE"),
        lambda: env.settle(second.id, second.razorpay_order_id, "pay_TWO"),
    )
    settled, refused = outcomes(results)

    assert not refused, [r.detail for r in refused]
    assert len(settled) == 2
    assert env.balance() == BALANCE - 4500.0

    paid = [txn for txn in env.transactions() if txn.status is TransactionStatus.PAID]
    assert len(paid) == 2
    assert all(txn.paid_at is not None for txn in paid)


def test_the_same_transaction_verified_twice_at_once_settles_once(
    env, slow_monthly_spend
):
    """The idempotent replay path, raced against itself: one settlement,
    one balance movement, and the second answer is "already done"."""
    txn = payable(env, product="widget_id", quantity=20)  # 2,000.00

    results = in_parallel(
        lambda: env.settle(txn.id, txn.razorpay_order_id, "pay_SAME"),
        lambda: env.settle(txn.id, txn.razorpay_order_id, "pay_SAME"),
    )
    settled, refused = outcomes(results)

    assert not refused
    assert sorted(r.already_verified for r in settled) == [False, True]
    assert env.balance() == BALANCE - 2000.0
    assert env.stock("widget_id") == STOCK - 20


# ---------------------------------------------------------------------------
# Approval: one human decision per transaction, even when two arrive together
# ---------------------------------------------------------------------------


@pytest.fixture()
def slow_approval_lookup(monkeypatch):
    """Widen the gap between finding the outstanding approval and
    resolving it -- the read-then-write at the heart of the approval
    transition."""
    real = approval_routes.unresolved_approval

    def slow(*args, **kwargs):
        result = real(*args, **kwargs)
        time.sleep(INTERLEAVE_DELAY)
        return result

    monkeypatch.setattr(approval_routes, "unresolved_approval", slow)


def test_approve_and_reject_at_once_resolve_the_transaction_only_once(
    env, slow_approval_lookup
):
    """A human approval and a human rejection racing on one transaction.

    Whichever wins, the transaction must end in exactly one of the two
    coherent states -- never carrying both decisions, and never producing
    a provider order the rejection believed it had cancelled.
    """
    env.set_limits(autonomous_limit=1000.0, monthly_cap=50000.0)

    quote_id = env.quote(quantity=60)  # 6,000.00, above the threshold
    pending = env.order(quote_id)
    assert pending.transaction.status is TransactionStatus.AWAITING_APPROVAL

    transaction_id = pending.transaction.id
    results = in_parallel(
        lambda: env.approve(transaction_id),
        lambda: env.reject(transaction_id),
    )
    resolved, refused = outcomes(results)

    assert len(resolved) == 1, "both decisions were applied to one transaction"
    assert len(refused) == 1
    assert refused[0].status_code == 409

    # Exactly one approval row, carrying exactly one human decision.
    approvals = env.approvals(transaction_id)
    assert len(approvals) == 1
    assert approvals[0].status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)

    winner = resolved[0]
    final = winner.transaction.status
    if approvals[0].status is ApprovalStatus.APPROVED:
        assert final is TransactionStatus.ORDER_CREATED
        assert len(env.razorpay.orders_created) == 1
    else:
        assert final is TransactionStatus.CANCELLED
        # A rejected purchase must leave no order behind it.
        assert env.razorpay.orders_created == []
