from pydantic import BaseModel, Field, model_validator

from backend.enums.PolicyDecision import PolicyDecision


class PolicyResult(BaseModel):
    """The structured output of the deterministic policy engine.

    Carries not just the verdict but the evidence behind it, so a later
    audit trail can explain *what was checked* and *why* -- not just a
    single free-text reason string.

    Invariant (enforced below): the decision is a pure function of the
    findings. It is structurally impossible to construct a PolicyResult
    that claims ALLOW while carrying hard violations.
    """

    decision: PolicyDecision
    hard_violations: list[str] = Field(default_factory=list)
    approval_reasons: list[str] = Field(default_factory=list)
    evaluated_rules: list[str] = Field(default_factory=list)

    # Stable machine-readable rule ids, positionally parallel to the
    # human-readable lists above. The prose is for the demo; these are what
    # an audit trail or a caller should branch on.
    violation_codes: list[str] = Field(default_factory=list)
    approval_codes: list[str] = Field(default_factory=list)

    @staticmethod
    def decide(
        hard_violations: list[str],
        approval_reasons: list[str],
    ) -> PolicyDecision:
        """Hard violations always outrank approval reasons."""
        if hard_violations:
            return PolicyDecision.BLOCK
        if approval_reasons:
            return PolicyDecision.REQUIRE_APPROVAL
        return PolicyDecision.ALLOW

    @model_validator(mode="after")
    def _decision_matches_findings(self) -> "PolicyResult":
        expected = PolicyResult.decide(self.hard_violations, self.approval_reasons)
        if self.decision is not expected:
            raise ValueError(
                f"PolicyResult decision {self.decision!r} contradicts its findings "
                f"(expected {expected!r}): "
                f"{len(self.hard_violations)} hard violation(s), "
                f"{len(self.approval_reasons)} approval reason(s)."
            )
        return self

    @classmethod
    def from_findings(
        cls,
        *,
        hard_violations: list[str],
        approval_reasons: list[str],
        evaluated_rules: list[str],
        violation_codes: list[str] | None = None,
        approval_codes: list[str] | None = None,
    ) -> "PolicyResult":
        return cls(
            decision=cls.decide(hard_violations, approval_reasons),
            hard_violations=hard_violations,
            approval_reasons=approval_reasons,
            evaluated_rules=evaluated_rules,
            violation_codes=violation_codes or [],
            approval_codes=approval_codes or [],
        )

    @property
    def summary(self) -> str:
        """Compact human-readable audit line for the transaction record."""
        if self.hard_violations:
            detail = "; ".join(self.hard_violations)
        elif self.approval_reasons:
            detail = "; ".join(self.approval_reasons)
        else:
            detail = "All policy rules passed."
        return f"{self.decision.value.upper()}: {detail}"

    @property
    def codes(self) -> list[str]:
        """The codes that actually drove the decision."""
        return self.violation_codes if self.hard_violations else self.approval_codes
