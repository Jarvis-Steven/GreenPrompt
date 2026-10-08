"""Owner: backend member.

Request/response models taken from docs/api-contract.md (version 1).
Field names are shared with every team member: do not rename them without
agreement from everyone.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Tier = Literal["small", "medium", "big"]
Difficulty = Literal["easy", "medium", "hard"]
Mode = Literal["smart", "pick"]
QualityStatus = Literal["passed", "failed", "unchecked"]
CallStatus = Literal["success", "error"]
# How the difficulty was decided, and what became of the classifier call.
ClassifierMethod = Literal["rules", "model", "fallback"]
ClassifierStatus = Literal["skipped", "success", "timeout", "error", "invalid_output"]


ErrorCode = Literal[
    "VALIDATION_ERROR",
    "MODEL_UNAVAILABLE",
    "NOT_IMPLEMENTED",
    "INTERNAL_ERROR",
]


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=2000)
    mode: Mode
    selected_model: Optional[Tier] = None

    # Trim before the length checks run, so "   " counts as empty.
    @field_validator("session_id", "prompt", mode="before")
    @classmethod
    def _trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_mode(self):
        if self.mode == "smart" and self.selected_model is not None:
            raise ValueError("selected_model must be null in smart mode")
        if self.mode == "pick" and self.selected_model is None:
            raise ValueError("selected_model is required in pick mode")
        return self


class Quality(BaseModel):
    status: QualityStatus
    reason: str


class Attempt(BaseModel):
    tier: Tier
    model_name: str
    status: CallStatus
    latency_ms: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    quality_status: QualityStatus
    quality_reason: str


class Classifier(BaseModel):
    """How this request's difficulty was decided. Top level, never inside attempts."""

    used: bool                       # true if a provider call was ATTEMPTED, failures included
    status: ClassifierStatus
    method: ClassifierMethod
    model_name: Optional[str] = None
    latency_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    difficulty: Difficulty
    reason: str


class Metrics(BaseModel):
    energy_wh: float
    co2_g: float
    water_ml: float
    cost_inr: float


class Impact(Metrics):
    # The contract shows "estimated": true on impact. Defaults to true if the
    # metrics component leaves it out; all figures are estimates.
    estimated: bool = True


class Summary(BaseModel):
    total_prompts: int
    small_model_percentage: float
    escalations: int
    cumulative_savings: Metrics


class ChatResponse(BaseModel):
    request_id: str
    session_id: str
    answer: str
    difficulty: Difficulty
    # Optional so an older frontend keeps working; the backend always fills it in.
    classifier: Optional[Classifier] = None
    initial_model: Tier
    final_model: Tier
    quality: Quality
    escalated: bool
    attempts: list[Attempt]
    impact: Impact
    baseline: Metrics
    savings: Metrics
    # PROPOSAL (metrics member): present only when the classifier made a real
    # model call. Additive; impact/baseline/savings keep their v1 meanings.
    classifier_overhead: Optional[Impact] = None
    impact_including_classifier: Optional[Impact] = None
    summary: Summary


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool


class ErrorResponse(BaseModel):
    request_id: str
    error: ErrorBody
