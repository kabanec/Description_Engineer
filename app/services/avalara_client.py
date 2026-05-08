"""Avalara HSAC + HSAC10 thin client for the DQA POC.

Adapted Avalara HSAC + HSAC10 client (single-step QUOTE_ENHANCED10 +
slimmed to the two methods we use:

  * ``classify_hsac()``   — single-step QUOTE_ENHANCED10 call, returns HS-10
  * ``classify_hsac10()`` — two-step:
        Step 1: POST /api/v2/hs/classify     (HS-6 candidates)
        Step 2: POST /api/v2/companies/{co}/quotes/create
                (QUOTE_MEDIAN with hs_code hint, returns HS-10)

Both return the same response shape:
    {
        "success": bool,
        "hs_code": str  (HS-10),
        "confidence": float,
        "classification_model": str,
        "step1_hs6_candidates": list (only HSAC10),
        "raw_latency_ms": int,
        "raw_response": dict,
        "error": Optional[str],
    }
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


_DEFAULT_BASE = "https://hsac-int.xbo.avalara.com"
_DEFAULT_HSAC_URL = "https://hsac-int.xbo.avalara.com"


def _norm_country(code: Optional[str]) -> str:
    if not code:
        return "US"
    code = code.strip().upper()
    return {"UK": "GB", "EN": "GB"}.get(code, code)


def _default_region(country: str) -> str:
    return {"US": "WA", "CA": "ON", "GB": "ENG"}.get(country, "")


class AvalaraClient:
    """Thin wrapper around Avalara HSAC + HSAC10 endpoints (POC use only)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        hsac_url: Optional[str] = None,
        company_id: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key or os.getenv("AVALARA_API_KEY", "")
        self.base_url = (base_url or os.getenv("AVALARA_BASE_URL", _DEFAULT_BASE)).rstrip("/")
        self.hsac_url = (hsac_url or os.getenv("AVALARA_HSAC_URL", _DEFAULT_HSAC_URL)).rstrip("/")
        self.company_id = company_id or os.getenv("AVALARA_COMPANY_ID", "0")
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "missing"

    # ─── Single-step (QUOTE_ENHANCED10) ────────────────────────────────

    def classify_hsac(
        self,
        description: str,
        ship_from: str = "US",
        ship_to: str = "US",
        coo: Optional[str] = None,
        currency: str = "USD",
        price: float = 10.0,
    ) -> Dict[str, Any]:
        if not self.is_configured:
            return {
                "success": False,
                "hs_code": "",
                "confidence": 0.0,
                "classification_model": "",
                "raw_latency_ms": 0,
                "raw_response": {},
                "error": "AVALARA_API_KEY not configured",
            }

        url = f"{self.base_url}/api/v2/companies/{self.company_id}/quotes/create"
        ship_from = _norm_country(ship_from)
        ship_to = _norm_country(ship_to)
        coo = _norm_country(coo) if coo else None

        class_params: List[Dict[str, str]] = [
            {"name": "price", "value": str(price), "unit": currency},
        ]
        if coo:
            class_params.append({"name": "coo", "value": coo})

        payload = {
            "id": str(uuid.uuid4()),
            "companyId": int(self.company_id) if str(self.company_id).isdigit() else 0,
            "currency": currency,
            "shipFrom": {"country": ship_from},
            "shipTo": {"country": ship_to, "region": _default_region(ship_to)},
            "type": "QUOTE_ENHANCED10",
            "taxRegistered": True,
            "lines": [{
                "lineNumber": 0,
                "quantity": 1,
                "item": {
                    "itemCode": "DQA-POC-1",
                    "description": description,
                    "classificationParameters": class_params,
                },
            }],
        }

        return self._post_quote(url, payload, "HSAC", coo=coo)

    # ─── Two-step (HS-6 candidates → QUOTE_MEDIAN refinement) ──────────

    def classify_hsac10(
        self,
        description: str,
        ship_from: str = "US",
        ship_to: str = "US",
        coo: Optional[str] = None,
        currency: str = "USD",
        price: float = 10.0,
    ) -> Dict[str, Any]:
        """Two-step: HS-6 candidates first (`/api/v2/hs/classify`), then
        HS-10 refinement (`QUOTE_MEDIAN` with `hs_code` hint)."""
        if not self.is_configured:
            return {
                "success": False,
                "hs_code": "",
                "confidence": 0.0,
                "classification_model": "",
                "step1_hs6_candidates": [],
                "raw_latency_ms": 0,
                "raw_response": {},
                "error": "AVALARA_API_KEY not configured",
            }

        # Step 1 — HS-6 candidates from the lightweight classify endpoint
        step1_url = f"{self.hsac_url}/api/v2/hs/classify"
        step1_payload = {
            "description": description,
            "country": _norm_country(ship_to),
        }
        t0 = time.time()
        candidates: List[Dict[str, Any]] = []
        step1_raw: Dict[str, Any] = {}
        try:
            r1 = requests.post(
                step1_url,
                json=step1_payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
            r1.raise_for_status()
            step1_raw = r1.json()
            for c in (step1_raw.get("classifications") or step1_raw.get("results") or []):
                hs = c.get("hs_code") or c.get("hsCode") or c.get("code") or ""
                hs = str(hs).replace(".", "").replace(" ", "")
                if hs:
                    candidates.append({
                        "hs6": hs[:6],
                        "confidence": float(c.get("confidence") or c.get("score") or 0),
                    })
        except Exception as exc:
            return {
                "success": False,
                "hs_code": "",
                "confidence": 0.0,
                "classification_model": "",
                "step1_hs6_candidates": candidates,
                "raw_latency_ms": int((time.time() - t0) * 1000),
                "raw_response": step1_raw,
                "error": f"step1_failed: {exc}",
            }

        if not candidates:
            return {
                "success": False,
                "hs_code": "",
                "confidence": 0.0,
                "classification_model": "",
                "step1_hs6_candidates": [],
                "raw_latency_ms": int((time.time() - t0) * 1000),
                "raw_response": step1_raw,
                "error": "no_hs6_candidates",
            }

        # Step 2 — refine top candidate to HS-10 via QUOTE_MEDIAN
        ship_from = _norm_country(ship_from)
        ship_to = _norm_country(ship_to)
        coo = _norm_country(coo) if coo else None
        top_hs6 = candidates[0]["hs6"]

        url = f"{self.base_url}/api/v2/companies/{self.company_id}/quotes/create"
        class_params: List[Dict[str, str]] = [
            {"name": "price", "value": str(price), "unit": currency},
            {"name": "hs_code", "value": top_hs6},
        ]
        if coo:
            class_params.append({"name": "coo", "value": coo})

        payload = {
            "id": str(uuid.uuid4()),
            "companyId": int(self.company_id) if str(self.company_id).isdigit() else 0,
            "currency": currency,
            "shipFrom": {"country": ship_from},
            "shipTo": {"country": ship_to, "region": _default_region(ship_to)},
            "type": "QUOTE_MEDIAN",
            "taxRegistered": True,
            "lines": [{
                "lineNumber": 0,
                "quantity": 1,
                "item": {
                    "itemCode": "DQA-POC-1",
                    "description": description,
                    "classificationParameters": class_params,
                },
            }],
        }
        result = self._post_quote(url, payload, "HSAC10", coo=coo)
        result["step1_hs6_candidates"] = candidates
        result["raw_latency_ms"] = int((time.time() - t0) * 1000)
        return result

    # ─── Shared quote POST ────────────────────────────────────────────

    def _post_quote(self, url: str, payload: Dict[str, Any], label: str, coo: Optional[str] = None) -> Dict[str, Any]:
        t0 = time.time()
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return {
                "success": False,
                "hs_code": "",
                "confidence": 0.0,
                "classification_model": "",
                "raw_latency_ms": int((time.time() - t0) * 1000),
                "raw_response": {},
                "error": f"{label.lower()}_request_failed: {exc}",
            }

        latency_ms = int((time.time() - t0) * 1000)

        # Quote responses have lines[0].item.classificationParameters[hs_code]
        # plus a classification_model field. Be defensive — Avalara payload
        # shapes vary across API versions.
        hs_code = ""
        confidence = 0.0
        model = ""
        try:
            line = (data.get("lines") or [{}])[0]
            item = line.get("item", {}) or {}
            for cp in item.get("classificationParameters", []) or []:
                if cp.get("name") == "hs_code":
                    hs_code = str(cp.get("value") or "").replace(".", "").replace(" ", "")
                if cp.get("name") in ("classification_model", "model"):
                    model = str(cp.get("value") or "")
            confidence = float(
                line.get("classificationConfidence")
                or item.get("confidence")
                or 0.0
            )
        except Exception:
            pass

        success = bool(hs_code)
        return {
            "success": success,
            "hs_code": hs_code,
            "confidence": confidence,
            "classification_model": model or label,
            "raw_latency_ms": latency_ms,
            "raw_response": data,
            "error": "" if success else f"{label.lower()}_no_hs_in_response",
        }
