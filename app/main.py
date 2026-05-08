"""Description Engineering POC FastAPI entry point.

Endpoints:
  POST /v1/engineer        rewrite a single raw description into a customs-grade one
  POST /v1/engineer-batch  up to 25 descriptions (each is a full rewrite loop)
  POST /v1/score-only      DQA score only, no rewriting, no Avalara (cheap diagnostic)
  GET  /v1/health          liveness + LLM + Avalara configuration status
  GET  /docs               Swagger UI
  GET  /redoc              ReDoc
  GET  /openapi.json       machine-readable spec

Algorithm (per call):
  1. Run dual-Avalara on original description (HSAC + HSAC10 in parallel)
  2. Run DQA on the original
  3. If DQA score >= target → short-circuit (already_compliant=true)
  4. Otherwise: LLM rewrite passes 1..N, scored by DQA between each pass.
     Stop when score >= target OR max_iterations OR improvement < min_improvement.
  5. Run dual-Avalara on the engineered description
  6. Compare HS-6 before/after; flag if changed.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import settings
from app.routers import engineer, meta
from app.services.avalara_client import AvalaraClient
from app.services.llm_client import LLMClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Description Engineering POC v%s starting up", __version__)
    llm = LLMClient(api_key=settings.anthropic_api_key, model=settings.llm_model)
    avc = AvalaraClient(
        api_key=settings.avalara_api_key,
        base_url=settings.avalara_base_url,
        hsac_url=settings.avalara_hsac_url,
        company_id=settings.avalara_company_id,
    )
    logger.info("LLM model: %s | configured: %s", settings.llm_model, llm.is_configured)
    logger.info("Avalara configured: %s", avc.is_configured)
    logger.info("Prompt version: %s", llm.prompt_version)
    logger.info("DQA mode: %s", settings.dqa_mode)
    if not llm.is_configured:
        logger.warning("ANTHROPIC_API_KEY not set — /v1/engineer will return 503.")
    if not avc.is_configured:
        logger.warning("AVALARA_API_KEY not set — /v1/engineer will return 503.")
    yield
    logger.info("Description Engineering POC shutting down")


app = FastAPI(
    title="Description Engineering POC — Customs-Grade Description Rewriter",
    description=(
        "Standalone POC: rewrites raw customer product descriptions into "
        "customs-grade descriptions using a Claude Haiku rewrite-with-DQA-feedback "
        "loop. Verifies HS-6 stability via parallel Avalara HSAC + HSAC10 calls "
        "before and after engineering. Companion service to dqa-poc and coo-poc. "
        "See README.md for the algorithm walkthrough and BRD reference."
    ),
    version=__version__,
    lifespan=lifespan,
)

cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(engineer.router)
app.include_router(meta.router)


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "desc-engineering-poc",
        "version": __version__,
        "docs": "/docs",
        "health": "/v1/health",
    }
