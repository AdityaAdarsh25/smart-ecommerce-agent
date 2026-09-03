"""The one critical section every money action passes through.

MandatePay's financial checks are all read-then-write: read the current
world, decide, then write the decision. Between the read and the write
another request can slip past the same check, because a check that has
not been committed yet is invisible to it. That is how two concurrent
`/create-order` calls both cleared duplicate detection, and how two
concurrent `/payment/verify` calls could both read the same monthly spend
and both settle.

There is exactly ONE lock here on purpose. Per-route locks would not fix
anything: the requests that race are in *different* routes, and two locks
serialize nothing between them. Every money action -- order creation,
approval execution, payment settlement -- takes this same lock, so their
read/decide/commit sections are ordered with respect to each other.

    with money_critical_section():
        ...read state, decide, commit the decision...

It is an `RLock` so a section may nest inside another (the approval path
reaches into helpers shared with order creation) without self-deadlock.
Only this lock is ever held, and nothing acquires it while holding a
database write, so there is no lock ordering to get wrong.

SCOPE, HONESTLY STATED
----------------------

This protects the supported v1 runtime: ONE FastAPI/Uvicorn process. It
is a `threading` primitive and is therefore invisible to any other
process. A horizontally scaled, multi-worker or multi-process deployment
would need database-backed serialization -- `SELECT ... FOR UPDATE`, a
row-level advisory lock, or an equivalent -- instead of this in-process
critical section. Nothing here should be read as a claim to protect that
deployment.
"""

import threading

# Module level, so every importer shares the same object. Importing the
# accessor rather than the lock itself keeps that from being accidentally
# rebound to a private one.
_MONEY_LOCK = threading.RLock()


def money_critical_section() -> threading.RLock:
    """The shared money lock, as a context manager.

    Hold it across a read -> decide -> commit section, and release it
    before any external network call: the provider round trip does not
    need protecting, and holding a process-wide lock across it would
    serialize every buyer behind the slowest one.
    """
    return _MONEY_LOCK


__all__ = ["money_critical_section"]
