# Description Engineering POC — Customs-Grade Description Rewriter

A standalone proof-of-concept service that rewrites raw customer product descriptions into the "ideal" customs-grade equivalent — concise, customs-broker-friendly text that maximizes HS-classification reliability — using a Claude Haiku rewrite-with-DQA-feedback loop. Each call also runs Avalara HSAC + Avalara HSAC10 on both the original and engineered descriptions to verify HS-6 stability.

Prepared as a demo for Avalara. Companion to dqa-poc, coo-poc, and dps-poc (separate repos).

---

## 1. What this service does

The DQA POC tells you **how bad** a description is. This service tells you **how to fix it** — and produces the fixed version.

Given a raw description (often vague, marketing-flavored, or incomplete), this service produces ONE engineered description that:

- Surfaces the customs-relevant facts (material, construction, form, intended use, technical specs) the original buried or omitted
- Strips marketing fluff, brand-mood adjectives, and SKU codes that don't help classification
- Does NOT invent attributes not in or implied by the original
- Does NOT change what the product fundamentally IS (verified by HS-6 stability check)
- Does NOT copy HTS schedule language verbatim (CBP-prohibited practice)
- Reaches DQA score ≥ 75 (grade B or better) — high enough that downstream classifiers converge

The three POC services tell a coherent end-to-end story for an Avalara reviewer:

| Step | Service | What it does |
|---|---|---|
| 1 | `dqa-poc` | **Score** the customer's raw description; surface what's wrong |
| 2 | `desc-engineering-poc` | **Fix** the description with measurable DQA improvement and a stable HS-6 |
| 3 | `coo-poc` | **Validate** the declared country of origin against the (now-improved) description and trade-data signal |

---

## 2. Algorithm

```
RAW DESCRIPTION
     │
     ├─►  dual-Avalara (HSAC + HSAC10) ─► classifier_results_before
     │
     ├─►  DQA score ─► dqa_before
     │
     ├─►  if dqa_before.score >= TARGET (default 75)  ─► return as-is (already_compliant)
     │
     └─►  ITERATIVE REWRITE LOOP:
              Pass 1: LLM rewrite (raw + chapter hint)        ─►  candidate
                      DQA score(candidate)                    ─►  cand_dqa
                      if cand_dqa.score >= TARGET             ─►  STOP (target_reached)
              Pass 2: LLM refine (previous + dqa.suggestions) ─►  candidate
                      DQA score(candidate)
                      if improvement < MIN_IMPROVEMENT (5)    ─►  STOP (no_improvement)
              ...
              Pass N: max iterations reached                  ─►  STOP (max_iterations)
              
     ─►  dual-Avalara on engineered description ─► classifier_results_after
     ─►  DQA score on engineered description    ─► dqa_after
     ─►  hs6_changed = HS6_before ≠ HS6_after   (warning if true)
     ─►  return full trace + before/after DQA + before/after Avalara + cost estimate
```

**Stop conditions** (whichever fires first):
- `target_reached` — DQA score ≥ `target_score` (default 75)
- `no_improvement` — last pass improved by < `min_improvement` (default 5 points)
- `max_iterations` — pass count reached `max_iterations` (default 3)
- `already_compliant` — original score ≥ target, no LLM call made
- `llm_error` — Anthropic call raised; return best candidate so far

---

## 3. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/engineer` | Single raw description → engineered description + before/after diagnostics |
| `POST` | `/v1/engineer-batch` | Up to 25 descriptions, each gets a full rewrite loop |
| `POST` | `/v1/score-only` | DQA score only (no rewriting, no Avalara) — diagnostic / sanity-check |
| `GET` | `/v1/health` | Liveness + LLM + Avalara configuration status |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/redoc` | ReDoc UI |
| `GET` | `/openapi.json` | Machine-readable spec |

### Sample request — `POST /v1/engineer`

```bash
curl -s http://localhost:8002/v1/engineer \
  -H 'Content-Type: application/json' \
  -d '{
    "description": "stuff for hair",
    "hs_chapter_hint": "33",
    "target_score": 75,
    "max_iterations": 3
  }' | jq
```

### Sample response (truncated)

```json
{
  "original_description": "stuff for hair",
  "engineered_description": "hair styling cream, leave-in conditioning preparation for personal use, non-animal origin, 100 ml plastic tube",
  "already_compliant": false,
  "iteration_count": 2,
  "iteration_trace": [
    {"pass": 0, "description": "stuff for hair", "dqa_score": 18, "dqa_grade": "F", "dqa_suggestions": ["Replace vague terms (stuff)...", "Description is only 3 word(s)..."]},
    {"pass": 1, "description": "hair conditioning cream...", "dqa_score": 62, "dqa_grade": "C", "llm_latency_ms": 940},
    {"pass": 2, "description": "hair styling cream, leave-in...", "dqa_score": 78, "dqa_grade": "B", "llm_latency_ms": 1010}
  ],
  "dqa_before": {"...full DQA result for original...": "..."},
  "dqa_after":  {"...full DQA result for engineered...": "..."},
  "avalara_before": {
    "hsac":   {"hs_code": "...", "confidence": 0.31},
    "hsac10": {"hs_code": "...", "confidence": 0.42},
    "agreement": {"hs2": false, "hs4": false, "hs6": false, "hs10": false}
  },
  "avalara_after": {
    "hsac":   {"hs_code": "3305900000", "confidence": 0.88},
    "hsac10": {"hs_code": "3305900000", "confidence": 0.91},
    "agreement": {"hs2": true, "hs4": true, "hs6": true, "hs10": true}
  },
  "hs6_changed": false,
  "warnings": [],
  "prompt_version": "system=v1.0.0,rewrite=v1.0.0,refine=v1.0.0",
  "cost_estimate_usd": 0.002,
  "stop_reason": "target_reached"
}
```

---

## 4. Running the demo

### Prerequisites
- Python 3.10+
- Anthropic API key (`ANTHROPIC_API_KEY`)
- Avalara HSAC credentials (`AVALARA_API_KEY`, `AVALARA_COMPANY_ID`)

### One-time setup

```bash
cd ~/desc-engineering-poc
pip3 install -r requirements.txt --break-system-packages

cat > .env <<EOF
ANTHROPIC_API_KEY=sk-ant-...
AVALARA_API_KEY=...
AVALARA_COMPANY_ID=...
AVALARA_BASE_URL=https://hsac-int.xbo.avalara.com
AVALARA_HSAC_URL=https://hsac-int.xbo.avalara.com
EOF
```

### Launch

```bash
python3 run.py
```

You should see:
```
INFO Description Engineering POC v1.0.0 starting up
INFO LLM model: claude-haiku-4-5-20251001 | configured: True
INFO Avalara configured: True
INFO Prompt version: system=v1.0.0,rewrite=v1.0.0,refine=v1.0.0
INFO uvicorn.error — Uvicorn running on http://0.0.0.0:8002
```

### Demo

- Swagger UI: <http://localhost:8002/docs>
- Health: <http://localhost:8002/v1/health>

---

## 5. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Claude SDK auth — paid API |
| `LLM_MODEL` | `claude-haiku-4-5-20251001` | Override to `claude-sonnet-4-6` for higher quality at higher cost |
| `LLM_MAX_TOKENS` | `400` | Cap rewrite length (descriptions are short — 400 is plenty) |
| `LLM_TEMPERATURE` | `0.2` | Low for deterministic customs descriptions |
| `LLM_TIMEOUT` | `30` | Per-call timeout (seconds) |
| `AVALARA_API_KEY` | (required) | HSAC + HSAC10 |
| `AVALARA_COMPANY_ID` | `0` | Avalara company ID for `/quotes/create` |
| `AVALARA_BASE_URL` | `https://hsac-int.xbo.avalara.com` | Quote endpoint base |
| `AVALARA_HSAC_URL` | `https://hsac-int.xbo.avalara.com` | HS-classify endpoint base |
| `DQA_MODE` | `embedded` | `embedded` (in-process) or `http` (call dqa-poc) |
| `DQA_URL` | `http://localhost:8001/v1/analyze` | Used when `DQA_MODE=http` |
| `TARGET_SCORE` | `75` | Loop stops when DQA score ≥ this |
| `MAX_ITERATIONS` | `3` | Hard cap on LLM rewrite passes |
| `MIN_IMPROVEMENT` | `5` | Stop if a pass improved by < N points vs previous |
| `MAX_BATCH_SIZE` | `25` | Reject batches > N (lower than DQA — each item is heavier) |
| `COST_PER_LLM_PASS_USD` | `0.001` | Used in `cost_estimate_usd` (informational) |
| `COST_PER_AVALARA_PAIR_USD` | `0.0` | Zero per Avalara demo audience |
| `HOST` / `PORT` | `0.0.0.0` / `8002` | uvicorn bind |
| `CORS_ORIGINS` | `*` | Comma-separated allowlist |
| `RELOAD` | `1` | Set `0` for production |

---

## 6. Demo audience and cost notes

This POC is prepared for Avalara internal review. **Avalara cost is zero in this context** (HSAC + HSAC10 are Avalara products). **Anthropic Claude IS a paid API** — each `/v1/engineer` call costs roughly:

```
cost ≈ N_passes * $0.001   (Haiku, default model)
```

A typical rewrite uses 1–3 passes, so per-call LLM cost is **$0.001 to $0.003**. A 25-item batch is roughly $0.025 to $0.075. The exact figure is reported in `cost_estimate_usd` on every response.

---

## 7. The system prompt

The LLM is instructed to be conservative — improve the description by surfacing customs-relevant attributes that are present or strongly implied, but never invent. The prompt template is at `prompts/system_prompt_v1.0.0.txt`. Refinement passes use `prompts/refine_prompt_v1.0.0.txt` and feed back the DQA suggestions from the previous pass.

The active prompt version is reported in every response under `prompt_version` so an Avalara reviewer can trace which version produced any output.

The **no-fabrication guardrail** is encoded in the system prompt. It is a soft guardrail (LLM-enforced), not a hard one — see BR-DE-12 in the BRD.

---

## 8. Limitations and notes

- **No-fabrication is a soft guardrail.** The LLM is instructed not to invent material/brand/country attributes, but cannot be hard-prevented. v1.1 may add a post-rewrite verifier.
- **HS-6 stability is informational in v1.0.** If the rewrite changes the HS-6 returned by Avalara, `hs6_changed: true` is flagged and a warning is added — but the response is still returned. v1.1 may add a `strict_mode` that rejects HS-changing rewrites.
- **No persistent ProductCatalog** — historical-consistency signal returns a neutral 50 (same as dqa-poc).
- **No PII storage** — every request is in-memory. Descriptions are not logged to disk.
- **No auth** — open API, suitable for local demos.
- **DQA embedded by default** — to call a sibling `dqa-poc` service instead, set `DQA_MODE=http` and `DQA_URL=http://localhost:8001/v1/analyze`.

---

## 9. License and provenance

Algorithm derived from a production tariff-engineering kernel, simplified to a single rewrite-with-feedback loop (vs. multi-alternative generation). DQA analyzer copied from `dqa-poc/`. Avalara client from a production reference.

For the full BRD, contact the project maintainer.
