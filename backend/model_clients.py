"""Owner: backend member. Real provider calls, with errors normalized.

One adapter for any OpenAI-compatible chat endpoint (Groq, Gemini's OpenAI
mode, Ollama, ...). Each tier is configured from environment variables
(see backend/.env.example). Nothing here ever returns canned text: a failure
raises ProviderError and the caller records an honest error attempt.
"""
import time

import httpx

from backend.config import (
    classifier_tier, classifier_timeout_seconds, get_tier_config, provider_timeout_seconds,
)


class ProviderError(Exception):
    """A provider call failed. Carries what the attempt record needs."""

    def __init__(self, code: str, message: str, *, retryable: bool,
                 model_name: str = "", latency_ms: int = 0):
        super().__init__(message)
        self.code = code            # CONFIG_MISSING, TIMEOUT, RATE_LIMITED, AUTH_FAILED, ...
        self.message = message
        self.retryable = retryable
        self.model_name = model_name
        self.latency_ms = latency_ms


def _make_client() -> httpx.AsyncClient:
    """Separate function so tests can swap in a fake transport."""
    return httpx.AsyncClient(timeout=provider_timeout_seconds())


def _token_count(usage: dict, key: str):
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) else None


async def call_model(tier: str, prompt: str, *, instructions: str | None = None,
                     timeout: float | None = None, max_tokens: int | None = None) -> dict:
    """Return answer, model_name, latency_ms, input_tokens, output_tokens.

    Token counts are None when the provider does not report them.

    The optional keyword arguments exist for the difficulty classifier and do
    nothing unless passed, so the original two-argument call is unchanged:
      instructions  system message sent before the prompt
      timeout       total deadline for this one request (no automatic retry)
      max_tokens    cap on the reply length
    """
    cfg = get_tier_config(tier)
    if not cfg.configured:
        raise ProviderError(
            "CONFIG_MISSING",
            f"The {tier} tier has no provider settings (set {tier.upper()}_BASE_URL, "
            f"{tier.upper()}_API_KEY and {tier.upper()}_MODEL).",
            retryable=False, model_name=cfg.model,
        )

    started = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    messages = [{"role": "user", "content": prompt}]
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    payload = {"model": cfg.model, "messages": messages}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    extra = {} if timeout is None else {"timeout": timeout}

    try:
        async with _make_client() as client:
            response = await client.post(
                f"{cfg.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                json=payload,
                **extra,
            )
    except httpx.TimeoutException:
        raise ProviderError("TIMEOUT", f"The {tier} model timed out.", retryable=True,
                            model_name=cfg.model, latency_ms=elapsed_ms())
    except httpx.HTTPError as exc:
        raise ProviderError("NETWORK_ERROR", f"Could not reach the {tier} provider ({type(exc).__name__}).",
                            retryable=True, model_name=cfg.model, latency_ms=elapsed_ms())

    latency_ms = elapsed_ms()
    status = response.status_code
    if status in (401, 403):
        raise ProviderError("AUTH_FAILED", f"The {tier} provider rejected the API key (HTTP {status}).",
                            retryable=False, model_name=cfg.model, latency_ms=latency_ms)
    if status == 429:
        raise ProviderError("RATE_LIMITED", f"The {tier} provider rate limit was hit.",
                            retryable=True, model_name=cfg.model, latency_ms=latency_ms)
    if status >= 400:
        detail = response.text[:200].replace("\n", " ")
        raise ProviderError("PROVIDER_ERROR", f"The {tier} provider returned HTTP {status}: {detail}",
                            retryable=status >= 500, model_name=cfg.model, latency_ms=latency_ms)

    try:
        data = response.json()
        answer = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError("BAD_RESPONSE", f"The {tier} provider returned an unexpected response.",
                            retryable=True, model_name=cfg.model, latency_ms=latency_ms)
    if not isinstance(answer, str) or not answer.strip():
        raise ProviderError("EMPTY_ANSWER", f"The {tier} provider returned an empty answer.",
                            retryable=True, model_name=cfg.model, latency_ms=latency_ms)

    usage = data.get("usage") or {}
    return {
        "answer": answer,
        "model_name": cfg.model,
        "latency_ms": latency_ms,
        "input_tokens": _token_count(usage, "prompt_tokens"),
        "output_tokens": _token_count(usage, "completion_tokens"),
    }


async def classify(prompt: str, *, instructions: str, tier: str | None = None,
                   timeout: float | None = None) -> dict:
    """One classifier call on CLASSIFIER_TIER. Same return shape as call_model.

    "answer" is the model's raw text; Jarvis's parser turns it into a difficulty.
    A single attempt only: on failure this raises ProviderError and the caller
    falls back. It never retries and never invents a difficulty.
    """
    return await call_model(
        tier or classifier_tier(),
        prompt,
        instructions=instructions,
        timeout=classifier_timeout_seconds() if timeout is None else timeout,
    )
