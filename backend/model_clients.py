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


# ---------------------------------------------------------------------------
# PROPOSED (bring your own key): native Anthropic Messages API.
# Anthropic documents its OpenAI-compatibility layer as "primarily intended to
# test and compare model capabilities, and is not considered a long-term or
# production-ready solution", so Claude is called natively instead of through
# that shim. Verified against platform.claude.com/docs/en/api/messages.
# ---------------------------------------------------------------------------
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_HOST = "api.anthropic.com"
ANTHROPIC_MAX_TOKENS = 4096


def is_anthropic(base_url: str) -> bool:
    return ANTHROPIC_HOST in (base_url or "")


def _status_to_error(tier: str, status: int, detail: str, model: str, latency_ms: int):
    """Map a provider HTTP status onto our existing ProviderError codes."""
    if status in (401, 403):
        return ProviderError("AUTH_FAILED", f"The {tier} provider rejected the API key (HTTP {status}).",
                             retryable=False, model_name=model, latency_ms=latency_ms)
    if status == 429:
        return ProviderError("RATE_LIMITED", f"The {tier} provider rate limit was hit.",
                             retryable=True, model_name=model, latency_ms=latency_ms)
    return ProviderError("PROVIDER_ERROR", f"The {tier} provider returned HTTP {status}: {detail}",
                         retryable=status >= 500, model_name=model, latency_ms=latency_ms)


async def _call_anthropic(cfg, tier: str, prompt: str, instructions, timeout, max_tokens) -> dict:
    """One POST /v1/messages call. The key travels in a header, never a URL."""
    payload = {
        "model": cfg.model,
        "max_tokens": max_tokens or ANTHROPIC_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if instructions:
        payload["system"] = instructions
    headers = {
        "x-api-key": cfg.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    extra = {} if timeout is None else {"timeout": timeout}

    started = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        async with _make_client() as client:
            response = await client.post(f"{cfg.base_url}/messages", headers=headers,
                                         json=payload, **extra)
    except httpx.TimeoutException:
        raise ProviderError("TIMEOUT", f"The {tier} model timed out.", retryable=True,
                            model_name=cfg.model, latency_ms=elapsed_ms())
    except httpx.HTTPError as exc:
        raise ProviderError("NETWORK_ERROR",
                            f"Could not reach the {tier} provider ({type(exc).__name__}).",
                            retryable=True, model_name=cfg.model, latency_ms=elapsed_ms())

    latency_ms = elapsed_ms()
    if response.status_code >= 400:
        # Never surface the provider's raw body verbatim; it can echo the request.
        raise _status_to_error(tier, response.status_code,
                               response.text[:200].replace("\n", " "), cfg.model, latency_ms)

    try:
        data = response.json()
        blocks = data.get("content") or []
        answer = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    except (ValueError, AttributeError, TypeError):
        raise ProviderError("BAD_RESPONSE", f"The {tier} provider returned an unexpected response.",
                            retryable=True, model_name=cfg.model, latency_ms=latency_ms)
    if not answer.strip():
        raise ProviderError("EMPTY_ANSWER", f"The {tier} provider returned an empty answer.",
                            retryable=True, model_name=cfg.model, latency_ms=latency_ms)

    usage = data.get("usage") or {}
    return {
        "answer": answer,
        "model_name": cfg.model,
        "latency_ms": latency_ms,
        # Anthropic reports input_tokens / output_tokens directly.
        "input_tokens": _token_count(usage, "input_tokens"),
        "output_tokens": _token_count(usage, "output_tokens"),
    }


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

    if is_anthropic(cfg.base_url):
        return await _call_anthropic(cfg, tier, prompt, instructions, timeout, max_tokens)

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


# ---------------------------------------------------------------------------
# PROPOSED: cheap read-only key check. Lists models; never generates text,
# never returns the key or the provider's raw body.
# ---------------------------------------------------------------------------
VERIFY_ENDPOINTS = {
    # provider -> (url, header builder)
    "openai": ("https://api.openai.com/v1/models",
               lambda key: {"Authorization": f"Bearer {key}"}),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/models",
               lambda key: {"Authorization": f"Bearer {key}"}),
    "anthropic": ("https://api.anthropic.com/v1/models",
                  lambda key: {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}),
}


async def verify_key(provider: str) -> dict:
    """{"ok": bool, "code": str, "message": str} - safe to return to the UI."""
    from backend import runtime_keys

    target = VERIFY_ENDPOINTS.get(provider)
    if target is None:
        return {"ok": False, "code": "UNKNOWN_PROVIDER", "message": "Unknown provider."}
    key = runtime_keys.get_key(provider)
    if not key:
        return {"ok": False, "code": "NO_KEY", "message": "No key is stored for this provider."}

    url, headers_for = target
    try:
        async with _make_client() as client:
            response = await client.get(url, headers=headers_for(key), timeout=20)
    except httpx.TimeoutException:
        return {"ok": False, "code": "TIMEOUT", "message": "The provider did not respond in time."}
    except httpx.HTTPError as exc:
        return {"ok": False, "code": "NETWORK_ERROR",
                "message": f"Could not reach the provider ({type(exc).__name__})."}

    if response.status_code == 200:
        return {"ok": True, "code": "OK", "message": "The key works."}
    if response.status_code in (401, 403):
        return {"ok": False, "code": "AUTH_FAILED", "message": "The provider rejected this key."}
    if response.status_code == 429:
        return {"ok": False, "code": "RATE_LIMITED", "message": "The provider rate limit was hit."}
    # Deliberately no provider body: it can echo request content.
    return {"ok": False, "code": "PROVIDER_ERROR",
            "message": f"The provider returned HTTP {response.status_code}."}
