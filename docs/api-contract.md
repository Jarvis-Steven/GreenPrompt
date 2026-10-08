# Shared API contract — version 1

This matches the team master prompts. These interfaces are planned; the scaffold only implements health and a 501 chat response.

## POST /chat

```json
{"session_id":"browser-generated-uuid","prompt":"What is 1 + 1?","mode":"smart","selected_model":null}
```

- Trim prompt; require 1–2,000 characters.
- mode: smart or pick. selected_model: null for smart, small/medium/big for pick.
- Frontend generates a session UUID at startup/reset and sends one request at a time.

Successful response example (illustrative metrics):

```json
{
  "request_id": "server-generated-uuid",
  "session_id": "browser-generated-uuid",
  "answer": "2",
  "difficulty": "easy",
  "initial_model": "small",
  "final_model": "small",
  "quality": {"status": "passed", "reason": "Arithmetic result verified."},
  "escalated": false,
  "attempts": [{
    "tier": "small",
    "model_name": "actual-provider-model",
    "status": "success",
    "latency_ms": 420,
    "input_tokens": 12,
    "output_tokens": 3,
    "quality_status": "passed",
    "quality_reason": "Arithmetic result verified."
  }],
  "impact": {"estimated": true, "energy_wh": 0.03, "co2_g": 0.02181, "water_ml": 0.03, "cost_inr": 0.002},
  "baseline": {"energy_wh": 0.30, "co2_g": 0.2181, "water_ml": 0.30, "cost_inr": 0.030},
  "savings": {"energy_wh": 0.27, "co2_g": 0.19629, "water_ml": 0.27, "cost_inr": 0.028},
  "summary": {
    "total_prompts": 1,
    "small_model_percentage": 100,
    "escalations": 0,
    "cumulative_savings": {"energy_wh": 0.27, "co2_g": 0.19629, "water_ml": 0.27, "cost_inr": 0.028}
  }
}
```

Difficulty: easy/medium/hard. Tier: small/medium/big. Attempt status: success/error. Quality status: passed/failed/unchecked. Success means a provider answered, not that the answer is correct. Token counts may be null. Errors use quality_status=unchecked with a reason.

Every attempted call must be recorded. escalated means a higher tier was attempted. final_model is the producer of the returned answer, even if subsequent attempts failed. Preserve the actual quality status. If all calls fail, return a non-2xx error. Never silently substitute mock content.

```json
{"request_id":"server-generated-uuid","error":{"code":"MODEL_UNAVAILABLE","message":"No model could complete this request.","retryable":true}}
```

Normalize validation errors to the same envelope. No streaming: frontend shows a generic pending state until completion.

## Internal functions

- router.classify_prompt(prompt) -> difficulty
- router.choose_model(difficulty, mode, selected_model) -> tier
- validator.check_answer(prompt, answer) -> {status, reason}
- router.next_model(current_tier, quality_status, call_status) -> tier or None
- await model_clients.call_model(tier, prompt) -> {answer, model_name, latency_ms, input_tokens, output_tokens}; backend normalizes provider errors into attempts.
- metrics.calculate_metrics(attempts) -> {impact, baseline, savings}
- storage.record_result(record) -> summary

Completed storage records contain request_id, session_id, prompt, mode, difficulty, initial_model, final_model, answer, quality, escalated, attempts, impact, baseline, savings. Store once per request_id.

## Accounting

Illustrative per-call energy/cost: Small 0.03 Wh / INR 0.002; Medium 0.12 Wh / INR 0.010; Big 0.30 Wh / INR 0.030. CO2 factor 0.727 g/Wh; water factor 1 mL/Wh. Not measurements or actual provider prices.

Sum all attempts, including errors, using full tier estimates as a simplifying assumption. Baseline is one Big call per completed prompt. Savings = baseline minus impact. Preserve negatives and full calculation precision.

Summary includes completed requests returning an answer in that session only. Count escalated prompts once, not tier changes. Small percentage uses final_model. Deduplicate request IDs. Zero prompts yields zeros. Requests with no returned answer are excluded from MVP savings aggregates and can be logged separately.
