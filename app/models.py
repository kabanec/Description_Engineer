"""Pydantic schemas for the Description Engineering POC API."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class EngineerRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=2000)
    hs_chapter_hint: Optional[str] = Field(None, description="2-digit HS chapter (e.g. '33', '85').")
    current_hs10: Optional[str] = Field(None, description="Optional caller-provided HS-10 seed.")
    target_score: Optional[int] = Field(None, ge=50, le=95)
    max_iterations: Optional[int] = Field(None, ge=1, le=5)


class EngineerBatchRequest(BaseModel):
    items: List[EngineerRequest] = Field(..., min_length=1, max_length=25)


class IterationStep(BaseModel):
    model_config = {"protected_namespaces": ()}

    pass_: int = Field(..., alias="pass")
    description: str
    dqa_score: int
    dqa_grade: str
    dqa_suggestions: List[str]
    llm_latency_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class EngineerResponse(BaseModel):
    original_description: str
    engineered_description: str
    already_compliant: bool
    iteration_count: int
    iteration_trace: List[Dict[str, Any]]
    dqa_before: Dict[str, Any]
    dqa_after: Dict[str, Any]
    avalara_before: Dict[str, Any]
    avalara_after: Dict[str, Any]
    hs6_changed: bool
    warnings: List[str] = Field(default_factory=list)
    prompt_version: str
    cost_estimate_usd: float
    stop_reason: str


class EngineerBatchResponse(BaseModel):
    results: List[EngineerResponse]
    total_iterations: int
    total_cost_estimate_usd: float


class ScoreOnlyRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=2000)
    hs_chapter_hint: Optional[str] = None


class ScoreOnlyResponse(BaseModel):
    quality_score: int
    grade: str
    grade_label: str
    signals: Dict[str, Any]
    recommendations: List[str]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    llm_configured: bool
    avalara_configured: bool
    dqa_mode: str
    prompt_version: str
    version: str
    error: Optional[str] = None
