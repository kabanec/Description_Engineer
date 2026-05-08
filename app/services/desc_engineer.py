"""Description Engineering — rewrite loop with DQA feedback + before/after Avalara HS-6 stability check.

Loop:
  pass 0: score the original description (DQA + dual Avalara). Short-circuit
          if it already meets ``target_score`` (already_compliant=True).
  pass 1: LLM rewrite from raw + (optionally) DQA suggestions from pass 0.
  pass N: LLM refinement using previous attempt + new DQA suggestions.
          Stop when score >= target OR iterations >= max OR
          improvement < min_improvement vs previous pass.

Final response includes:
  * iteration_trace — every pass with description + DQA score + suggestions
  * dqa_before / dqa_after — full DQA results pre/post engineering
  * avalara_before / avalara_after — both Avalara classifiers + agreement on
    the original AND the engineered description
  * hs6_changed — flag when HS-6 differs between before/after
  * cost_estimate_usd — sum of LLM passes (Avalara assumed zero per BRD §6)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.avalara_client import AvalaraClient
from app.services.dqa_runner import run_dqa_embedded, run_dqa_http
from app.services.dual_classifier import run_dual_avalara
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)


def _trim(hs: str, n: int) -> str:
    return (hs or "").replace(".", "").replace(" ", "")[:n]


def _score_with_dqa(description: str, classifier_results: List[Dict]) -> Dict[str, Any]:
    """Run DQA on a description (embedded vs http chosen at config)."""
    if settings.dqa_mode == "http":
        return run_dqa_http(description, settings.dqa_url, timeout=settings.dqa_timeout)
    return run_dqa_embedded(description, classifier_results=classifier_results)


def engineer_description(
    description: str,
    hs_chapter_hint: Optional[str] = None,
    current_hs10: Optional[str] = None,
    target_score: Optional[int] = None,
    max_iterations: Optional[int] = None,
    avalara_client: Optional[AvalaraClient] = None,
    llm_client: Optional[LLMClient] = None,
) -> Dict[str, Any]:
    """Rewrite a raw description into a customs-grade description.

    Returns a payload with iteration_trace, dqa_before, dqa_after,
    avalara_before, avalara_after, hs6_changed, warnings, prompt_version,
    cost_estimate_usd.
    """
    target = target_score or settings.target_score
    max_iter = max_iterations or settings.max_iterations
    min_imp = settings.min_improvement

    avc = avalara_client or AvalaraClient(
        api_key=settings.avalara_api_key,
        base_url=settings.avalara_base_url,
        hsac_url=settings.avalara_hsac_url,
        company_id=settings.avalara_company_id,
        timeout=settings.avalara_timeout,
    )
    llm = llm_client or LLMClient(
        api_key=settings.anthropic_api_key,
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
    )

    warnings: List[str] = []

    # ── Pass 0: baseline ────────────────────────────────────────────
    classifier_results_before, hsac_before, hsac10_before, agreement_before = run_dual_avalara(
        description=description,
        ship_from=settings.default_ship_from,
        ship_to=settings.default_ship_to,
        client=avc,
    )
    dqa_before = _score_with_dqa(description, classifier_results_before)

    iteration_trace: List[Dict[str, Any]] = [{
        "pass": 0,
        "description": description,
        "dqa_score": int(dqa_before["quality_score"]),
        "dqa_grade": dqa_before["grade"],
        "dqa_suggestions": dqa_before.get("recommendations", []),
        "llm_latency_ms": None,
    }]

    # Short-circuit: original already good enough
    if dqa_before["quality_score"] >= target:
        return {
            "original_description": description,
            "engineered_description": description,
            "already_compliant": True,
            "iteration_count": 0,
            "iteration_trace": iteration_trace,
            "dqa_before": dqa_before,
            "dqa_after": dqa_before,
            "avalara_before": {"hsac": hsac_before, "hsac10": hsac10_before, "agreement": agreement_before},
            "avalara_after":  {"hsac": hsac_before, "hsac10": hsac10_before, "agreement": agreement_before},
            "hs6_changed": False,
            "warnings": warnings,
            "prompt_version": llm.prompt_version,
            "cost_estimate_usd": 0.0,
            "stop_reason": "already_compliant",
        }

    # ── Iterative rewrite passes ────────────────────────────────────
    if not llm.is_configured:
        warnings.append("ANTHROPIC_API_KEY not set — returning original description unchanged.")
        return {
            "original_description": description,
            "engineered_description": description,
            "already_compliant": False,
            "iteration_count": 0,
            "iteration_trace": iteration_trace,
            "dqa_before": dqa_before,
            "dqa_after": dqa_before,
            "avalara_before": {"hsac": hsac_before, "hsac10": hsac10_before, "agreement": agreement_before},
            "avalara_after":  {"hsac": hsac_before, "hsac10": hsac10_before, "agreement": agreement_before},
            "hs6_changed": False,
            "warnings": warnings,
            "prompt_version": llm.prompt_version,
            "cost_estimate_usd": 0.0,
            "stop_reason": "llm_unavailable",
        }

    current_desc = description
    current_score = int(dqa_before["quality_score"])
    last_dqa = dqa_before
    llm_passes = 0
    stop_reason = "max_iterations"

    for pass_idx in range(1, max_iter + 1):
        try:
            if pass_idx == 1:
                rewrite = llm.rewrite(
                    description=description,
                    hs_chapter_hint=hs_chapter_hint,
                    current_hs10=current_hs10,
                )
            else:
                rewrite = llm.refine(
                    original_description=description,
                    previous_description=current_desc,
                    previous_score=current_score,
                    dqa_suggestions=last_dqa.get("recommendations", []),
                    hs_chapter_hint=hs_chapter_hint,
                )
        except Exception as exc:
            warnings.append(f"LLM call failed on pass {pass_idx}: {exc}")
            stop_reason = "llm_error"
            break

        llm_passes += 1
        candidate = rewrite["text"]

        # Score the candidate (no Avalara call inside the loop — saves tokens
        # and we'll run Avalara once at the end for before/after)
        cand_dqa = _score_with_dqa(candidate, classifier_results=classifier_results_before)
        cand_score = int(cand_dqa["quality_score"])

        iteration_trace.append({
            "pass": pass_idx,
            "description": candidate,
            "dqa_score": cand_score,
            "dqa_grade": cand_dqa["grade"],
            "dqa_suggestions": cand_dqa.get("recommendations", []),
            "llm_latency_ms": rewrite.get("latency_ms"),
            "input_tokens": rewrite.get("input_tokens"),
            "output_tokens": rewrite.get("output_tokens"),
        })

        improvement = cand_score - current_score
        current_desc = candidate
        last_dqa = cand_dqa
        prev_score = current_score
        current_score = cand_score

        if cand_score >= target:
            stop_reason = "target_reached"
            break
        if pass_idx > 1 and improvement < min_imp:
            stop_reason = "no_improvement"
            break

    # ── Final pass: re-run dual Avalara on the engineered description ─
    classifier_results_after, hsac_after, hsac10_after, agreement_after = run_dual_avalara(
        description=current_desc,
        ship_from=settings.default_ship_from,
        ship_to=settings.default_ship_to,
        client=avc,
    )
    dqa_after = _score_with_dqa(current_desc, classifier_results_after)

    hs6_before_top = _trim(agreement_before["hsac_hs10"] or agreement_before["hsac10_hs10"], 6)
    hs6_after_top = _trim(agreement_after["hsac_hs10"] or agreement_after["hsac10_hs10"], 6)
    hs6_changed = bool(hs6_before_top and hs6_after_top and hs6_before_top != hs6_after_top)
    if hs6_changed:
        warnings.append(
            f"HS-6 changed during engineering: {hs6_before_top} -> {hs6_after_top}. "
            "v1.0 is informational only; review before shipping."
        )

    cost_estimate = (
        llm_passes * settings.cost_per_llm_pass_usd
        + 2 * settings.cost_per_avalara_pair_usd  # before + after
    )

    return {
        "original_description": description,
        "engineered_description": current_desc,
        "already_compliant": False,
        "iteration_count": llm_passes,
        "iteration_trace": iteration_trace,
        "dqa_before": dqa_before,
        "dqa_after": dqa_after,
        "avalara_before": {"hsac": hsac_before, "hsac10": hsac10_before, "agreement": agreement_before},
        "avalara_after":  {"hsac": hsac_after,  "hsac10": hsac10_after,  "agreement": agreement_after},
        "hs6_changed": hs6_changed,
        "warnings": warnings,
        "prompt_version": llm.prompt_version,
        "cost_estimate_usd": round(cost_estimate, 4),
        "stop_reason": stop_reason,
    }
