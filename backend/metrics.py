"""Owner: metrics member. Illustrative estimates, not measured usage.

Accounting rules (see docs/metrics-assumptions.md):
- Every attempted call, including provider errors and failed quality checks,
-is charged the full per-tier estimate.
- Baseline is one Big call per completed prompt.
- Savings = baseline - impact; negatives are preserved, never clamped.
- Full precision is kept; the frontend rounds only for display.
"""
import math

TIER_ESTIMATES = {
    "small": {"energy_wh": 0.03, "cost_inr": 0.002},
    "medium": {"energy_wh": 0.12, "cost_inr": 0.010},
    "big": {"energy_wh": 0.30, "cost_inr": 0.030},
}
CO2_G_PER_WH = 0.727
WATER_ML_PER_WH = 1.0

METRIC_KEYS = ("energy_wh", "co2_g", "water_ml", "cost_inr")
TIER_RANK = {"small": 0, "medium": 1, "big": 2}


def _field(obj, key, default=None):
    """Read a field from a dict-like or attribute-style object (e.g. a Pydantic model)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _derive(energy_wh: float, cost_inr: float) -> dict:
    return {
        "energy_wh": energy_wh,
        "co2_g": energy_wh * CO2_G_PER_WH,
        "water_ml": energy_wh * WATER_ML_PER_WH,
        "cost_inr": cost_inr,
    }


# PROPOSAL (branch proposed-fixes, by the backend member).
# The hybrid difficulty classifier makes a REAL extra model call on roughly
# 60% of requests. In a live run on 2026-10-09 that was 2,830 real tokens,
# 27.4% of all tokens spent, and none of it appeared in attempts[], so it was
# billed 0.00 Wh and "savings" were overstated. The classifier runs on the
# CLASSIFIER_TIER (small by default), so one small-tier estimate is used.
CLASSIFIER_ESTIMATE_TIER = "small"


def classifier_overhead(classifier) -> dict | None:
    """Estimated cost of the classifier's own model call, or None if it never ran.

    ESTIMATE, like every figure here: one CLASSIFIER_ESTIMATE_TIER call, using
    the same contract constants as attempts. "used" is true whenever a provider
    call was ATTEMPTED, so a failed classifier call is charged too -- the same
    rule attempts already follow.
    """
    if classifier is None:
        return None
    if not _field(classifier, "used"):
        return None
    estimate = TIER_ESTIMATES[CLASSIFIER_ESTIMATE_TIER]
    return {"estimated": True, **_derive(estimate["energy_wh"], estimate["cost_inr"])}


def calculate_metrics(attempts: list[dict], *, classifier=None) -> dict:
    """Return impact, baseline, savings; include every attempt and negatives.

    Raises ValueError for an empty attempt list or an unknown tier. A completed
    request always has at least one attempt, so empty input signals a caller bug.

    PROPOSAL: the optional keyword-only `classifier` is additive. When it is
    None (the default, and what every existing caller does) the returned dict
    is byte-identical to before. When a classifier call was attempted, two
    EXTRA keys appear: "classifier_overhead" and "impact_including_classifier".
    "impact", "baseline" and "savings" keep their existing definitions exactly,
    so the v1 contract and every existing test are unaffected.
    """
    attempts = list(attempts or [])
    if not attempts:
        raise ValueError("a completed request must contain at least one attempt")

    energy_wh = 0.0
    cost_inr = 0.0
    for index, attempt in enumerate(attempts):
        tier = _field(attempt, "tier")
        if tier not in TIER_ESTIMATES:
            raise ValueError(
                f"attempt {index}: invalid tier {tier!r}; expected one of {sorted(TIER_ESTIMATES)}"
            )
        energy_wh += TIER_ESTIMATES[tier]["energy_wh"]
        cost_inr += TIER_ESTIMATES[tier]["cost_inr"]

    impact = {"estimated": True, **_derive(energy_wh, cost_inr)}
    big = TIER_ESTIMATES["big"]
    baseline = _derive(big["energy_wh"], big["cost_inr"])
    savings = {key: baseline[key] - impact[key] for key in METRIC_KEYS}
    result = {"impact": impact, "baseline": baseline, "savings": savings}

    overhead = classifier_overhead(classifier)
    if overhead is not None:
        result["classifier_overhead"] = overhead
        result["impact_including_classifier"] = {
            "estimated": True,
            **_derive(energy_wh + overhead["energy_wh"], cost_inr + overhead["cost_inr"]),
        }
    return result


def verify_record(record: dict) -> list[str]:
    """Integration aid: list inconsistencies between a backend record and its attempts.

    Returns an empty list when consistent. This only reports; it never changes data.
    Used by the integration checklist ("savings match the actual attempts").
    """
    problems = []
    attempts = list(_field(record, "attempts") or [])
    if not attempts:
        return ["record has no attempts"]
    try:
        expected = calculate_metrics(attempts)
    except ValueError as exc:
        return [str(exc)]

    for section in ("impact", "baseline", "savings"):
        supplied = _field(record, section)
        if supplied is None:
            problems.append(f"{section} is missing")
            continue
        for key in METRIC_KEYS:
            got = _field(supplied, key)
            want = expected[section][key]
            if got is None or not math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12):
                problems.append(f"{section}.{key}: record has {got!r}, attempts imply {want!r}")

    tiers = [_field(a, "tier") for a in attempts]
    initial = _field(record, "initial_model")
    final = _field(record, "final_model")

    if tiers[0] != initial:
        problems.append(f"initial_model is {initial!r} but the first attempt used {tiers[0]!r}")
    ranks = [TIER_RANK[t] for t in tiers]
    if any(b <= a for a, b in zip(ranks, ranks[1:])):
        problems.append(f"attempt tiers {tiers} are not strictly increasing")

    should_escalate = initial in TIER_RANK and any(r > TIER_RANK[initial] for r in ranks)
    if bool(_field(record, "escalated")) != should_escalate:
        problems.append(
            f"escalated is {_field(record, 'escalated')!r} but attempts imply {should_escalate!r}"
        )

    producers = [a for a in attempts if _field(a, "tier") == final and _field(a, "status") == "success"]
    if not producers:
        problems.append(f"final_model {final!r} has no successful attempt that could have produced the answer")
    else:
        quality = _field(record, "quality")
        status = quality if isinstance(quality, str) else _field(quality, "status")
        attempt_status = _field(producers[0], "quality_status")
        if status != attempt_status:
            problems.append(
                f"quality status {status!r} differs from the producing attempt's {attempt_status!r}"
            )
    return problems


# ---------------------------------------------------------------------------
# Extra calculations (additive; NOT part of the v1 API contract).
# Same rules apply: illustrative estimates, never measurements.
# ---------------------------------------------------------------------------
# Rough, editable conversion constants for relatable equivalents (illustrative only).
LED_BULB_WATTS = 10.0       # one 10 W LED bulb
PHONE_CHARGE_WH = 15.0      # one smartphone full charge (rough)
TEASPOON_ML = 4.93          # one teaspoon of water
CAR_G_CO2_PER_KM = 120.0    # rough petrol-car emissions per km


def _estimate(tier: str, key: str) -> float:
    if tier not in TIER_ESTIMATES:
        raise ValueError(f"invalid tier {tier!r}; expected one of {sorted(TIER_ESTIMATES)}")
    return TIER_ESTIMATES[tier][key]


def split_useful_and_wasted(record: dict) -> dict:
    """Split a request's impact into the attempt that produced the returned answer ("useful")
    and every other attempt ("wasted": failed checks, provider errors, calls after the answer).

    The producing attempt is the last successful attempt whose tier equals final_model.
    If none exists, everything counts as wasted. Same accounting rule as calculate_metrics.
    """
    attempts = list(_field(record, "attempts") or [])
    final = _field(record, "final_model")
    producer = None
    for index, attempt in enumerate(attempts):
        if _field(attempt, "tier") == final and _field(attempt, "status") == "success":
            producer = index
    useful_e = useful_c = wasted_e = wasted_c = 0.0
    for index, attempt in enumerate(attempts):
        tier = _field(attempt, "tier")
        energy, cost = _estimate(tier, "energy_wh"), _estimate(tier, "cost_inr")
        if index == producer:
            useful_e, useful_c = useful_e + energy, useful_c + cost
        else:
            wasted_e, wasted_c = wasted_e + energy, wasted_c + cost
    total = useful_e + wasted_e
    return {
        "useful": _derive(useful_e, useful_c),
        "wasted": _derive(wasted_e, wasted_c),
        "wasted_share_of_energy": (wasted_e / total) if total else 0.0,
    }


def expected_cost(p_small: float, p_medium: float, start: str = "small",
                  metric: str = "energy_wh") -> float:
    """Expected per-prompt energy (or cost_inr) under the suggested ladder.

    p_small / p_medium = probability the answer passes the check when that tier is tried.
    Big always ends the ladder. Starting at medium ignores p_small; starting at big ignores both.
    A what-if model for explaining the trade-off, not a prediction of real behaviour.
    """
    if metric not in ("energy_wh", "cost_inr"):
        raise ValueError("metric must be 'energy_wh' or 'cost_inr'")
    for name, p in (("p_small", p_small), ("p_medium", p_medium)):
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
    small, medium, big = (_estimate(t, metric) for t in ("small", "medium", "big"))
    if start == "small":
        return small + (1 - p_small) * (medium + (1 - p_medium) * big)
    if start == "medium":
        return medium + (1 - p_medium) * big
    if start == "big":
        return big
    raise ValueError("start must be 'small', 'medium' or 'big'")


def expected_savings(p_small: float, p_medium: float, start: str = "small",
                     metric: str = "energy_wh") -> float:
    """Expected saving per prompt versus always-Big. Negative means the routing loses."""
    return _estimate("big", metric) - expected_cost(p_small, p_medium, start, metric)


def break_even_small_pass_rate(p_medium: float = 0.0, metric: str = "energy_wh") -> float:
    """Smallest chance that Small passes first try for Small-first routing to match always-Big.

    Returns 0.0 when Small-first wins even if Small always fails (then Medium is good enough).
    """
    if not 0.0 <= p_medium <= 1.0:
        raise ValueError("p_medium must be between 0 and 1")
    small, medium, big = (_estimate(t, metric) for t in ("small", "medium", "big"))
    worst_case_after_small = medium + (1 - p_medium) * big
    return max(0.0, 1.0 - (big - small) / worst_case_after_small)


def equivalents(amounts: dict) -> dict:
    """Relatable (illustrative) translations of energy_wh, co2_g and water_ml amounts.

    Accepts impact, baseline, savings or a cumulative-savings dict. Negative in, negative out.
    """
    return {
        "estimated": True,
        "led_bulb_seconds": amounts["energy_wh"] * 3600.0 / LED_BULB_WATTS,
        "phone_charges": amounts["energy_wh"] / PHONE_CHARGE_WH,
        "water_teaspoons": amounts["water_ml"] / TEASPOON_ML,
        "car_metres": amounts["co2_g"] / CAR_G_CO2_PER_KM * 1000.0,
    }


def average_savings_per_prompt(summary: dict) -> dict:
    """Average per-prompt savings from a session summary; zeros for an empty session."""
    total = summary["total_prompts"]
    cumulative = summary["cumulative_savings"]
    return {key: (cumulative[key] / total if total else 0.0) for key in METRIC_KEYS}


def project_savings(summary: dict, prompts: int) -> dict:
    """HYPOTHETICAL: scale this session's average per-prompt savings to `prompts` prompts.

    Only valid if future prompts resemble this session's mix. Always present it as a what-if.
    """
    if prompts < 0:
        raise ValueError("prompts must be zero or more")
    return {key: value * prompts for key, value in average_savings_per_prompt(summary).items()}
