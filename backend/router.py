"""Owner: Jarvis. Pure decision functions; no provider calls here."""


def classify_prompt(prompt: str) -> str:
    """Return easy, medium, or hard."""
    raise NotImplementedError


def choose_model(difficulty: str, mode: str, selected_model: str | None) -> str:
    """Return small, medium, or big; respect Pick mode's starting tier."""
    raise NotImplementedError


def next_model(current_tier: str, quality_status: str, call_status: str) -> str | None:
    """Decide escalation; no tier above big, no repeated tier attempts."""
    raise NotImplementedError
