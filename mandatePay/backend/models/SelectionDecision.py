from pydantic import BaseModel,Field
from backend.models.RankedCandidates import RankedCandidate
from backend.enums.SelectionAction import SelectionAction

class SelectionDecision(BaseModel):
    action: SelectionAction
    message:str

    selected: RankedCandidate | None = None
    alternatives: list[RankedCandidate] = Field(default_factory=list)


