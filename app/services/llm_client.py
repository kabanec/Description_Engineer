"""Anthropic Claude client for description rewriting.

Two prompt templates live in `prompts/`:
  * system_prompt_v1.0.0.txt
  * rewrite_prompt_v1.0.0.txt   (first pass)
  * refine_prompt_v1.0.0.txt    (DQA-feedback refinement passes)
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_VERSION_RE = re.compile(r"^# version:\s*(\S+)", re.MULTILINE)


def _load_prompt(name: str) -> Tuple[str, str]:
    """Load a prompt file and return (body_without_metadata, version)."""
    path = _PROMPT_DIR / name
    raw = path.read_text(encoding="utf-8")
    m = _VERSION_RE.search(raw)
    version = m.group(1) if m else "unknown"
    body = "\n".join(line for line in raw.splitlines() if not line.startswith("#")).strip()
    return body, version


class LLMClient:
    """Wraps the Anthropic SDK with prompt loading + cost-aware logging."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 400,
        temperature: float = 0.2,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

        sys_body, sys_ver = _load_prompt("system_prompt_v1.0.0.txt")
        rewrite_body, rewrite_ver = _load_prompt("rewrite_prompt_v1.0.0.txt")
        refine_body, refine_ver = _load_prompt("refine_prompt_v1.0.0.txt")

        self.system_prompt = sys_body
        self.rewrite_template = rewrite_body
        self.refine_template = refine_body
        self.prompt_version = f"system={sys_ver},rewrite={rewrite_ver},refine={refine_ver}"

        self._client = None  # lazily constructed

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _ensure_client(self):
        if self._client is None:
            try:
                import anthropic  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "anthropic SDK not installed — pip install -r requirements.txt"
                ) from exc
            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def rewrite(
        self,
        description: str,
        hs_chapter_hint: Optional[str] = None,
        current_hs10: Optional[str] = None,
    ) -> Dict:
        """First-pass rewrite — no DQA feedback yet."""
        if not self.is_configured:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        prompt = self.rewrite_template.format(
            description=description.strip(),
            hs_chapter_hint=hs_chapter_hint or "(none)",
            current_hs10=current_hs10 or "(none)",
        )
        return self._call(prompt)

    def refine(
        self,
        original_description: str,
        previous_description: str,
        previous_score: int,
        dqa_suggestions: List[str],
        hs_chapter_hint: Optional[str] = None,
    ) -> Dict:
        """Refinement pass — feed DQA suggestions back into the LLM."""
        if not self.is_configured:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        sugg_text = "\n".join(f"- {s}" for s in dqa_suggestions) or "(no suggestions)"
        prompt = self.refine_template.format(
            previous_description=previous_description.strip(),
            previous_score=previous_score,
            dqa_suggestions=sugg_text,
            original_description=original_description.strip(),
            hs_chapter_hint=hs_chapter_hint or "(none)",
        )
        return self._call(prompt)

    def _call(self, user_prompt: str) -> Dict:
        client = self._ensure_client()
        t0 = time.time()
        msg = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        latency_ms = int((time.time() - t0) * 1000)
        text = "".join(block.text for block in msg.content if hasattr(block, "text")).strip()
        # Strip surrounding quotes if the model added them
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return {
            "text": text,
            "model": self.model,
            "stop_reason": getattr(msg, "stop_reason", None),
            "input_tokens": getattr(msg.usage, "input_tokens", None) if hasattr(msg, "usage") else None,
            "output_tokens": getattr(msg.usage, "output_tokens", None) if hasattr(msg, "usage") else None,
            "latency_ms": latency_ms,
        }
