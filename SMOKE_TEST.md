# Description Engineering POC — Smoke Test Runbook

Six cases exercising vague / chapter-constrained / already-good / batch / score-only / failure modes.

## 0. Boot the service

```bash
cd ~/desc-engineering-poc
pip3 install -r requirements.txt --break-system-packages

cat > .env <<EOF
ANTHROPIC_API_KEY=sk-ant-your-key-here
AVALARA_API_KEY=your-avalara-token
AVALARA_COMPANY_ID=your-company-id
EOF

python3 run.py
```

Open <http://localhost:8002/docs> in a second tab.

---

## 1. Health check

```bash
curl -s http://localhost:8002/v1/health | jq
```

**Expect when configured:**
```
{"status":"ok","llm_configured":true,"avalara_configured":true,"dqa_mode":"embedded","prompt_version":"system=v1.0.0,...","version":"1.0.0"}
```

---

## 2. Vague description (should iterate to grade B)

```bash
curl -s http://localhost:8002/v1/engineer \
  -H 'Content-Type: application/json' \
  -d '{"description": "stuff for hair", "hs_chapter_hint": "33", "target_score": 75}' \
  | jq '{original: .original_description, engineered: .engineered_description, before: .dqa_before.quality_score, after: .dqa_after.quality_score, passes: .iteration_count, hs6_changed, stop_reason}'
```

**Expect:**
- `before <= 30`
- `after >= 70`  (allow a 5-point band for LLM variance)
- `passes` is 1, 2, or 3
- `engineered` is meaningfully expanded (8+ words, mentions material/use/form)
- `stop_reason` is `target_reached` or `no_improvement`

---

## 3. Already-good description (no-op short-circuit)

```bash
curl -s http://localhost:8002/v1/engineer \
  -H 'Content-Type: application/json' \
  -d '{"description": "men'\''s 100% cotton knit polo shirt, short sleeve, navy, machine washable, casual wear"}' \
  | jq '{already_compliant, passes: .iteration_count, stop_reason, engineered: .engineered_description}'
```

**Expect:**
- `already_compliant: true`
- `passes: 0`
- `stop_reason: "already_compliant"`
- `engineered == original`
- No LLM cost

---

## 4. Chapter-constrained rewrite

```bash
curl -s http://localhost:8002/v1/engineer \
  -H 'Content-Type: application/json' \
  -d '{"description": "earbuds", "hs_chapter_hint": "85"}' \
  | jq '{engineered: .engineered_description, hs6_changed, before: .dqa_before.quality_score, after: .dqa_after.quality_score}'
```

**Expect:**
- `engineered` mentions electronic / wireless / audio terms appropriate for chapter 85
- `hs6_changed: false` ideally — the rewrite shouldn't change the HS chapter
- DQA improvement of at least 30 points

---

## 5. Score-only (cheap diagnostic — no LLM, no Avalara)

```bash
curl -s http://localhost:8002/v1/score-only \
  -H 'Content-Type: application/json' \
  -d '{"description": "general accessories"}' \
  | jq '{score: .quality_score, grade, recs: .recommendations}'
```

**Expect:** low score (≤ 30), grade `D` or `F`, suggestions about vagueness and word count. Costs nothing — used by Avalara reviewers to sanity-check the embedded DQA scorer.

---

## 6. Batch — 10 samples

```bash
curl -s -X POST http://localhost:8002/v1/engineer-batch \
  -H 'Content-Type: application/json' \
  -d "$(cat samples/descriptions.json | jq '{items: .samples}')" \
  | jq '{count: (.results | length), total_iterations, total_cost: .total_cost_estimate_usd, scores_after: [.results[].dqa_after.quality_score]}'
```

**Expect:**
- `count: 10`
- `total_iterations` between 10 and 30 (varies — already-good ones short-circuit)
- `total_cost` under $0.05
- `scores_after` mostly ≥ 70

---

## 7. Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `503 ANTHROPIC_API_KEY not set` | env not loaded | check `.env`, restart |
| `503 AVALARA_API_KEY not set` | env not loaded | check `.env`, restart |
| `iteration_count: 0` and `engineered == original` (without `already_compliant`) | LLM call failed | check `warnings`; usually a transient Anthropic issue |
| `hs6_changed: true` | LLM rewrote into a different HS chapter | review the engineered text; v1.0 lets it through, v1.1 may reject |
| Score plateaus below target | LLM ran out of useful additions | `stop_reason` will be `no_improvement` — normal for already-decent inputs |
| `cost_estimate_usd` is exactly zero but you got a rewrite | bug or `cost_per_llm_pass_usd` is 0 in env | set `COST_PER_LLM_PASS_USD=0.001` |
