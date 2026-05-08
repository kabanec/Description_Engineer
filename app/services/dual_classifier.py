"""Run Avalara HSAC + HSAC10 in parallel and shape results for DQA.

The DQA scoring functions (``_score_consensus``, ``_score_confidence_spread``)
expect a list of dicts with keys ``classifier``, ``hs6``, ``hs_code``,
``confidence``. This module produces that list by calling both Avalara
classifiers concurrently.

It also computes the ``agreement`` block reported in the API response —
boolean per-level match across HS-2, HS-4, HS-6, and HS-10 between the
two Avalara classifiers.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from app.services.avalara_client import AvalaraClient

logger = logging.getLogger(__name__)


def _trim(hs: str, n: int) -> str:
    return (hs or "").replace(".", "").replace(" ", "")[:n]


def run_dual_avalara(
    description: str,
    ship_from: str = "US",
    ship_to: str = "US",
    coo: Optional[str] = None,
    client: Optional[AvalaraClient] = None,
) -> Tuple[List[Dict], Dict, Dict, Dict]:
    """Call HSAC and HSAC10 in parallel.

    Returns:
        classifier_results (for DQA scoring),
        hsac_response (full),
        hsac10_response (full),
        agreement_block ({hs2, hs4, hs6, hs10})
    """
    avc = client or AvalaraClient()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_hsac = pool.submit(
            avc.classify_hsac,
            description=description, ship_from=ship_from, ship_to=ship_to, coo=coo,
        )
        f_hsac10 = pool.submit(
            avc.classify_hsac10,
            description=description, ship_from=ship_from, ship_to=ship_to, coo=coo,
        )
        hsac = f_hsac.result()
        hsac10 = f_hsac10.result()

    classifier_results: List[Dict] = []
    if hsac.get("success") and hsac.get("hs_code"):
        classifier_results.append({
            "classifier": "avalara_hsac",
            "hs_code": hsac["hs_code"],
            "hs6": _trim(hsac["hs_code"], 6),
            "confidence": float(hsac.get("confidence") or 0.7),
        })
    if hsac10.get("success") and hsac10.get("hs_code"):
        classifier_results.append({
            "classifier": "avalara_hsac10",
            "hs_code": hsac10["hs_code"],
            "hs6": _trim(hsac10["hs_code"], 6),
            "confidence": float(hsac10.get("confidence") or 0.7),
        })

    a_hs = hsac.get("hs_code", "") if hsac.get("success") else ""
    b_hs = hsac10.get("hs_code", "") if hsac10.get("success") else ""

    agreement = {
        "hs2": bool(a_hs and b_hs and _trim(a_hs, 2) == _trim(b_hs, 2)),
        "hs4": bool(a_hs and b_hs and _trim(a_hs, 4) == _trim(b_hs, 4)),
        "hs6": bool(a_hs and b_hs and _trim(a_hs, 6) == _trim(b_hs, 6)),
        "hs10": bool(a_hs and b_hs and _trim(a_hs, 10) == _trim(b_hs, 10)),
        "hsac_hs10": a_hs,
        "hsac10_hs10": b_hs,
    }

    return classifier_results, hsac, hsac10, agreement
