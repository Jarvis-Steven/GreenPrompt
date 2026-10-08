"""Owner: backend member. Normalize real provider results and errors."""


async def call_model(tier: str, prompt: str) -> dict:
    """Return answer, model_name, latency_ms, input_tokens, output_tokens."""
    raise NotImplementedError("Connect the agreed model providers.")
