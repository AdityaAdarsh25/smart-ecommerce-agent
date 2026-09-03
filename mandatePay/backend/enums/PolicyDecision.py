from enum import Enum


class PolicyDecision(str, Enum):
    """What the deterministic policy engine authorizes.

    This answers ONLY: "Is the agent authorized to attempt this purchase?"

    It deliberately says nothing about whether money actually moved.
    That is `TransactionStatus`, and the two must never be conflated:

        ALLOW  !=  PAID
        ALLOW  ==  "you may now attempt payment"
    """

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"
