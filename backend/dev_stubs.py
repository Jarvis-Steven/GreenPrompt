"""Owner: backend member. DEVELOPMENT STUBS ONLY.

These stand in for teammates' unfinished functions so the backend loop can be
tested end to end. They are used only when GREENPROMPT_DEV_STUBS=1, and every
response that used one carries an X-GreenPrompt-Dev-Stubs header. They never
produce model answers: model calls are always real.

Delete a stub's use (and then this file) as each real component lands.
"""

TIERS = ["small", "medium", "big"]
DIFFICULTIES = ["easy", "medium", "hard"]

CLASSIFIER_INSTRUCTIONS = (
    "PLACEHOLDER INSTRUCTIONS - replace with router.CLASSIFIER_INSTRUCTIONS. "
    "Rate how hard the user's question is to answer correctly. "
    "Reply with exactly one word: easy, medium, or hard. No punctuation, no explanation."
)


def classify_prompt(prompt: str) -> str:
    words = prompt.lower()
    if len(prompt) > 300 or any(w in words for w in ("explain", "design", "write a", "essay", "why")):
        return "hard"
    if len(prompt) > 60:
        return "medium"
    return "easy"


def choose_model(difficulty: str, mode: str, selected_model):
    if mode == "pick":
        return selected_model
    return {"easy": "small", "medium": "medium", "hard": "big"}[difficulty]


def check_answer(prompt: str, answer: str) -> dict:
    # Honest: no real check happens here, so the status is "unchecked".
    return {"status": "unchecked", "reason": "DEV STUB: no real answer check was performed."}


def next_model(current_tier: str, quality_status: str, call_status: str):
    if quality_status != "failed" and call_status != "error":
        return None
    index = TIERS.index(current_tier)
    return TIERS[index + 1] if index + 1 < len(TIERS) else None


def calculate_metrics(attempts: list) -> dict:
    # Zeros on purpose: real numbers belong to the metrics member.
    zero = {"energy_wh": 0.0, "co2_g": 0.0, "water_ml": 0.0, "cost_inr": 0.0}
    return {"impact": {"estimated": True, **zero}, "baseline": dict(zero), "savings": dict(zero)}


_records: dict = {}   # request_id -> record (in memory only)


def record_result(record: dict) -> dict:
    _records[record["request_id"]] = record
    mine = [r for r in _records.values() if r["session_id"] == record["session_id"]]
    total = len(mine)
    small = sum(1 for r in mine if r["final_model"] == "small")
    savings = {key: sum(r["savings"][key] for r in mine)
               for key in ("energy_wh", "co2_g", "water_ml", "cost_inr")}
    return {
        "total_prompts": total,
        "small_model_percentage": (100.0 * small / total) if total else 0.0,
        "escalations": sum(1 for r in mine if r["escalated"]),
        "cumulative_savings": savings,
    }


def classify_with_rules(prompt: str) -> dict:
    """Jarvis's rules pass. difficulty None means "rules cannot decide, ask a model".

    Deliberately decides only the most obvious cases, so the model path is
    actually exercised during development.
    """
    text = prompt.strip()
    lowered = text.lower()
    if len(text) > 400:
        return {"difficulty": "hard", "reason": "DEV STUB: very long prompt."}
    if len(text) <= 60 and lowered.startswith(("what is", "who is", "when did", "where is")):
        return {"difficulty": "easy", "reason": "DEV STUB: short factual lookup."}
    return {"difficulty": None, "reason": "DEV STUB: no rule matched; a model decision is needed."}


def classify_from_model_output(prompt: str, raw_text: str) -> dict:
    """Jarvis's parser. Raises ValueError when the model output is unusable."""
    lowered = (raw_text or "").strip().lower()
    found = [(lowered.index(level), level) for level in DIFFICULTIES if level in lowered]
    if not found:
        raise ValueError("DEV STUB: the classifier output contained no difficulty word.")
    level = min(found)[1]
    return {"difficulty": level, "reason": f"DEV STUB: classifier output contained {level!r}."}
