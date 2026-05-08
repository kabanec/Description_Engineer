"""Meta route — /v1/health."""
from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.config import settings
from app.models import HealthResponse
from app.services.avalara_client import AvalaraClient
from app.services.llm_client import LLMClient

router = APIRouter(prefix="/v1", tags=["meta"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    llm = LLMClient(
        api_key=settings.anthropic_api_key,
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
    )
    avc = AvalaraClient(
        api_key=settings.avalara_api_key,
        base_url=settings.avalara_base_url,
        hsac_url=settings.avalara_hsac_url,
        company_id=settings.avalara_company_id,
        timeout=settings.avalara_timeout,
    )
    if not llm.is_configured or not avc.is_configured:
        missing = []
        if not llm.is_configured:
            missing.append("ANTHROPIC_API_KEY")
        if not avc.is_configured:
            missing.append("AVALARA_API_KEY")
        return HealthResponse(
            status="degraded",
            llm_configured=llm.is_configured,
            avalara_configured=avc.is_configured,
            dqa_mode=settings.dqa_mode,
            prompt_version=llm.prompt_version,
            version=__version__,
            error=f"Missing env vars: {', '.join(missing)}",
        )
    return HealthResponse(
        status="ok",
        llm_configured=True,
        avalara_configured=True,
        dqa_mode=settings.dqa_mode,
        prompt_version=llm.prompt_version,
        version=__version__,
    )
