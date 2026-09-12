"""API contracts for persisted Cortex Analysis runs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CortexAnalysisSourceDTO(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(alias="requestId")
    response_version: int = Field(alias="responseVersion", ge=1)
    response_name: str = Field(alias="responseName")


class CortexAnalysisUniqueInsightDTO(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    response_name: str = Field(alias="responseName")
    text: str


class CortexAnalysisDisagreementDTO(BaseModel):
    who: str
    text: str


class CortexAnalysisConfidenceDTO(BaseModel):
    level: Literal["limited", "moderate", "high"]
    reason: str


class CortexAnalysisRunDTO(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    analysis_id: str = Field(alias="analysisId")
    request_group_id: str = Field(alias="requestGroupId")
    session_id: str = Field(alias="sessionId")
    model: str
    recommended_answer: str = Field(alias="recommendedAnswer")
    agreements: list[str] = Field(default_factory=list)
    disagreements: list[CortexAnalysisDisagreementDTO] = Field(default_factory=list)
    disagreement_note: str | None = Field(default=None, alias="disagreementNote")
    unique_insights: list[CortexAnalysisUniqueInsightDTO] = Field(
        default_factory=list,
        alias="uniqueInsights",
    )
    confidence: CortexAnalysisConfidenceDTO
    verify: list[str] = Field(default_factory=list)
    high_stakes_domain: Literal["financial", "medical", "legal", "safety"] | None = Field(
        default=None, alias="highStakesDomain"
    )
    source_fingerprint: str = Field(alias="sourceFingerprint")
    source_responses: list[CortexAnalysisSourceDTO] = Field(
        default_factory=list,
        alias="sourceResponses",
    )
    combined_response_count: int = Field(alias="combinedResponseCount", ge=2, le=3)
    failed_response_count: int = Field(alias="failedResponseCount", ge=0)
    ai_credits: int = Field(default=0, alias="aiCredits", ge=0)
    credit_usage_estimated: bool = Field(
        default=False,
        alias="creditUsageEstimated",
    )
    created_at: str = Field(alias="createdAt")
    is_stale: bool = Field(default=False, alias="isStale")
