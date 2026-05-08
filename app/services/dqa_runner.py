"""DQA invocation — embedded (in-process) or HTTP (call dqa-poc service)."""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def run_dqa_embedded(description: str, classifier_results: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """In-process DQA call using the bundled analyzer."""
    from app.services.dqa import analyze_description
    return analyze_description(description=description, classifier_results=classifier_results or [])


def run_dqa_http(description: str, dqa_url: str, ship_from: str = "US", ship_to: str = "US",
                 timeout: int = 30) -> Dict[str, Any]:
    """HTTP call to a sibling dqa-poc service (e.g. localhost:8001)."""
    body = json.dumps({
        "description": description,
        "ship_from": ship_from,
        "ship_to": ship_to,
    }).encode()
    req = urllib.request.Request(
        dqa_url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    # Map dqa-poc /v1/analyze response -> the same shape analyze_description returns
    return {
        "quality_score": payload["score"],
        "grade": payload["grade"],
        "grade_label": payload["grade_label"],
        "signals": payload["signals"],
        "recommendations": payload["suggestions"],
        "recommended_hs_code": payload.get("recommended_hs_code"),
        "recommended_hs_confidence": payload.get("recommended_hs_confidence"),
    }
