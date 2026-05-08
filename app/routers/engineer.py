"""Engineer + score-only routes."""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.models import (
    EngineerBatchRequest,
    EngineerBatchResponse,
    EngineerRequest,
    EngineerResponse,
    ScoreOnlyRequest,
    ScoreOnlyResponse,
)
from app.services.avalara_client import AvalaraClient
from app.services.desc_engineer import engineer_description
from app.services.dqa_runner import run_dqa_embedded
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["engineer"])


def _llm() -> LLMClient:
    return LLMClient(
        api_key=settings.anthropic_api_key,
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
    )


def _avc() -> AvalaraClient:
    return AvalaraClient(
        api_key=settings.avalara_api_key,
        base_url=settings.avalara_base_url,
        hsac_url=settings.avalara_hsac_url,
        company_id=settings.avalara_company_id,
        timeout=settings.avalara_timeout,
    )


@router.post("/engineer", response_model=EngineerResponse)
def engineer(req: EngineerRequest) -> EngineerResponse:
    if not _llm().is_configured:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set.")
    if not _avc().is_configured:
        raise HTTPException(status_code=503, detail="AVALARA_API_KEY not set.")

    result = engineer_description(
        description=req.description,
        hs_chapter_hint=req.hs_chapter_hint,
        current_hs10=req.current_hs10,
        target_score=req.target_score,
        max_iterations=req.max_iterations,
        avalara_client=_avc(),
        llm_client=_llm(),
    )
    return EngineerResponse(**result)


@router.post("/engineer-batch", response_model=EngineerBatchResponse)
def engineer_batch(req: EngineerBatchRequest) -> EngineerBatchResponse:
    if not _llm().is_configured:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set.")
    if not _avc().is_configured:
        raise HTTPException(status_code=503, detail="AVALARA_API_KEY not set.")
    if len(req.items) > settings.max_batch_size:
        raise HTTPException(status_code=400, detail=f"Batch size > {settings.max_batch_size}")

    avc = _avc()
    llm = _llm()
    results: List[EngineerResponse] = []
    total_iter = 0
    total_cost = 0.0
    for item in req.items:
        out = engineer_description(
            description=item.description,
            hs_chapter_hint=item.hs_chapter_hint,
            current_hs10=item.current_hs10,
            target_score=item.target_score,
            max_iterations=item.max_iterations,
            avalara_client=avc,
            llm_client=llm,
        )
        total_iter += out["iteration_count"]
        total_cost += out["cost_estimate_usd"]
        results.append(EngineerResponse(**out))

    return EngineerBatchResponse(
        results=results,
        total_iterations=total_iter,
        total_cost_estimate_usd=round(total_cost, 4),
    )


@router.post("/score-only", response_model=ScoreOnlyResponse)
def score_only(req: ScoreOnlyRequest) -> ScoreOnlyResponse:
    """Convenience endpoint: DQA only, no rewriting, no Avalara.

    Useful as a sanity check that the embedded DQA scorer is working,
    and for callers that have already run their own classifier pipeline
    and just want a quality score.
    """
    dqa = run_dqa_embedded(req.description, classifier_results=[])
    return ScoreOnlyResponse(
        quality_score=int(dqa["quality_score"]),
        grade=dqa["grade"],
        grade_label=dqa["grade_label"],
        signals=dqa["signals"],
        recommendations=dqa["recommendations"],
    )
