"""Owner: metrics member. Illustrative estimates, not measured usage."""

TIER_ESTIMATES = {
    "small": {"energy_wh": 0.03, "cost_inr": 0.002},
    "medium": {"energy_wh": 0.12, "cost_inr": 0.010},
    "big": {"energy_wh": 0.30, "cost_inr": 0.030},
}
CO2_G_PER_WH = 0.727
WATER_ML_PER_WH = 1.0


def calculate_metrics(attempts: list[dict]) -> dict:
    """Return impact, baseline, savings; include every attempt and negatives."""
    raise NotImplementedError
