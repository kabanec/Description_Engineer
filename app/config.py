"""POC configuration — env-driven via pydantic-settings."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8002

    # Anthropic Claude
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 400
    llm_temperature: float = 0.2
    llm_timeout: int = 30

    # Avalara — same env as dqa-poc (cost is zero for the Avalara demo audience)
    avalara_api_key: str = ""
    avalara_base_url: str = "https://hsac-int.xbo.avalara.com"
    avalara_hsac_url: str = "https://hsac-int.xbo.avalara.com"
    avalara_company_id: str = "0"
    avalara_timeout: int = 30
    default_ship_from: str = "US"
    default_ship_to: str = "US"

    # DQA — embedded in-process for v1.0
    dqa_mode: str = "embedded"   # "embedded" | "http"
    dqa_url: str = "http://localhost:8001/v1/analyze"
    dqa_timeout: int = 30

    # Rewrite loop tunables
    target_score: int = 75
    max_iterations: int = 3
    min_improvement: int = 5

    # Caps
    max_batch_size: int = 25
    max_description_length: int = 2000

    # Pricing notes (informational, surfaced in cost_estimate_usd)
    cost_per_llm_pass_usd: float = 0.001   # ~Haiku rate
    cost_per_avalara_pair_usd: float = 0.0  # zero for Avalara audience

    cors_origins: str = "*"


settings = Settings()
