from enum import Enum


class AuditEventType(str, Enum):
    """The append-only vocabulary of things worth remembering.

    Deliberately small. Each value names a decision the system made or a
    state transition it performed -- never an internal step, never a
    prompt, never a secret. Two properties matter more than breadth:

      * The names are STABLE. A stored event is a historical record, so
        renaming a value would rewrite history that has already happened.
      * The events OBSERVE. Nothing in the audit path is consulted when
        deciding whether money may be spent; removing every event in this
        enum would change no financial outcome.
    """

    # --- The AI half: what was understood, and what was chosen ----------
    # Structured outcomes only. No prompts, no model replies, no reasoning.
    AGENT_REQUEST_INTERPRETED = "AGENT_REQUEST_INTERPRETED"
    AGENT_CLARIFICATION_REQUIRED = "AGENT_CLARIFICATION_REQUIRED"
    AGENT_NO_MATCH = "AGENT_NO_MATCH"
    AGENT_REQUEST_UNSUPPORTED = "AGENT_REQUEST_UNSUPPORTED"
    AGENT_PRODUCT_SELECTED = "AGENT_PRODUCT_SELECTED"
    # The model was unreachable or unreadable. Nothing was quoted.
    AGENT_REQUEST_FAILED = "AGENT_REQUEST_FAILED"

    # --- The priced offer ------------------------------------------------
    QUOTE_CREATED = "QUOTE_CREATED"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    # Stock gone, product gone, quote already used -- anything that is not
    # expiry or drift.
    QUOTE_REVALIDATION_FAILED = "QUOTE_REVALIDATION_FAILED"
    # The catalogue price moved after the quote was taken. Recorded with
    # both numbers, because "which price" is the whole question.
    PRICE_DRIFT_DETECTED = "PRICE_DRIFT_DETECTED"

    # --- The deterministic verdict ---------------------------------------
    # One event per outcome rather than a generic POLICY_EVALUATED plus a
    # field: the decision is the thing being audited.
    POLICY_ALLOWED = "POLICY_ALLOWED"
    POLICY_APPROVAL_REQUIRED = "POLICY_APPROVAL_REQUIRED"
    POLICY_BLOCKED = "POLICY_BLOCKED"

    # --- The human in the loop -------------------------------------------
    APPROVAL_CREATED = "APPROVAL_CREATED"
    APPROVAL_APPROVED = "APPROVAL_APPROVED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    # A human said yes and a hard rule said no afterwards. Both facts are
    # kept: the approval event above is never retracted.
    APPROVAL_REVALIDATION_FAILED = "APPROVAL_REVALIDATION_FAILED"

    # --- The provider ----------------------------------------------------
    ORDER_CREATION_ATTEMPTED = "ORDER_CREATION_ATTEMPTED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_CREATION_FAILED = "ORDER_CREATION_FAILED"

    # --- Payment ---------------------------------------------------------
    PAYMENT_VERIFICATION_ATTEMPTED = "PAYMENT_VERIFICATION_ATTEMPTED"
    PAYMENT_VERIFIED = "PAYMENT_VERIFIED"
    PAYMENT_VERIFICATION_FAILED = "PAYMENT_VERIFICATION_FAILED"

    # --- Terminal financial truth ----------------------------------------
    # TRANSACTION_PAID is deliberately distinct from PAYMENT_VERIFIED: the
    # first is "the signature was ours", the second is "balance and stock
    # actually moved".
    TRANSACTION_PAID = "TRANSACTION_PAID"
    TRANSACTION_BLOCKED = "TRANSACTION_BLOCKED"
    TRANSACTION_CANCELLED = "TRANSACTION_CANCELLED"
    TRANSACTION_FAILED = "TRANSACTION_FAILED"
