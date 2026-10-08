"""Owner: Jarvis. Supported deterministic checks; no false correctness claims."""


def check_answer(prompt: str, answer: str) -> dict:
    """Return {status: passed|failed|unchecked, reason: str}."""
    raise NotImplementedError
