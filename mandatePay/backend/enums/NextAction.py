from enum import Enum

from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.SelectionAction import SelectionAction


class NextAction(str, Enum):
    """What the orchestrating client should do next.

    Derived server-side from the selection outcome and the deterministic
    policy verdict. The LLM never chooses this value -- it is a pure
    function of state the server established itself.

    It is an instruction to the caller, not a claim about money. In
    particular CREATE_ORDER means "you may now attempt payment"; it does
    not mean anything has been paid.
    """

    # Policy said ALLOW: call POST /app/v1/create-order with the quote.
    CREATE_ORDER = "create_order"

    # Policy said REQUIRE_APPROVAL: a human must resolve the approval
    # opened by create-order before a provider order can exist.
    AWAIT_APPROVAL = "await_approval"

    # Nothing further is possible on this request -- BLOCK, no match, or
    # an unsupported request shape.
    STOP = "stop"

    # The buyer must answer a question before anything can be quoted.
    CLARIFY = "clarify"


# The policy verdict -> next action mapping, in one place so no caller
# has to re-derive it. Note there is no entry that can turn BLOCK into a
# payable action.
_POLICY_NEXT_ACTION = {
    PolicyDecision.ALLOW: NextAction.CREATE_ORDER,
    PolicyDecision.REQUIRE_APPROVAL: NextAction.AWAIT_APPROVAL,
    PolicyDecision.BLOCK: NextAction.STOP,
}


def next_action_for(
    selection_action: SelectionAction,
    policy_decision: PolicyDecision | None,
) -> NextAction:
    """Deterministic. No AI output is consulted.

    A selection that produced no quote produces no payable action, and a
    quote that was BLOCKED produces STOP -- there is no path through this
    function from BLOCK to CREATE_ORDER.
    """
    if selection_action is SelectionAction.PROPOSE:
        if policy_decision is None:
            raise ValueError("A proposed purchase must carry a policy decision.")
        return _POLICY_NEXT_ACTION[policy_decision]

    if selection_action is SelectionAction.CLARIFY:
        return NextAction.CLARIFY

    # NO_MATCH and UNSUPPORTED: nothing to buy, nothing to ask.
    return NextAction.STOP
