"""Owner: backend member. PROPOSED "bring your own key" runtime key store.

IN MEMORY ONLY. Keys live in this process and nowhere else:
  - never written to disk, never added to backend/.env
  - never logged, never returned in a response, never in an exception message
  - __repr__ and __str__ deliberately emit only the masked form, so a key
    cannot leak through a traceback, a debugger, or a failed assertion.

Clearing is immediate and the process restart loses everything, which is the
intended behaviour for a demo machine.
"""
import threading

# GreenPrompt's fixed tier -> provider mapping for user-supplied keys.
# Each tier uses that provider's model AT THAT TIER from model_catalog.json.
TIER_PROVIDER = {"small": "gemini", "medium": "openai", "big": "anthropic"}
PROVIDERS = tuple(sorted(set(TIER_PROVIDER.values())))

# Catalog uses "google" for Gemini; the user-facing provider name is "gemini".
CATALOG_PROVIDER = {"gemini": "google", "openai": "openai", "anthropic": "anthropic"}

MIN_KEY_LENGTH = 16
MAX_KEY_LENGTH = 512


class InvalidKey(ValueError):
    """Raised when a submitted key fails validation.

    The message never contains the key or any part of it.
    """


def _validate(provider: str, api_key: str) -> str:
    if provider not in PROVIDERS:
        raise InvalidKey(f"unknown provider; expected one of {', '.join(PROVIDERS)}")
    if not isinstance(api_key, str):
        raise InvalidKey("api_key must be a string")
    key = api_key.strip()
    if not key:
        raise InvalidKey("api_key is empty")
    if len(key) < MIN_KEY_LENGTH:
        raise InvalidKey(f"api_key is shorter than {MIN_KEY_LENGTH} characters")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidKey(f"api_key is longer than {MAX_KEY_LENGTH} characters")
    # Provider keys are printable ASCII without spaces. Reject anything else
    # rather than passing odd bytes to a provider or into a header.
    if any(ch.isspace() for ch in key) or not all(33 <= ord(ch) <= 126 for ch in key):
        raise InvalidKey("api_key contains spaces or non-printable characters")
    return key


def mask(api_key: str) -> str:
    """Last four characters only, never more."""
    if not api_key:
        return ""
    return f"...{api_key[-4:]}" if len(api_key) > 4 else "..."


class _KeyStore:
    """Thread-safe in-memory store. Its repr is always masked."""

    def __init__(self):
        self._lock = threading.Lock()
        self._keys: dict = {}

    def set(self, provider: str, api_key: str) -> str:
        key = _validate(provider, api_key)
        with self._lock:
            self._keys[provider] = key
        return mask(key)

    def clear(self, provider: str) -> bool:
        if provider not in PROVIDERS:
            raise InvalidKey(f"unknown provider; expected one of {', '.join(PROVIDERS)}")
        with self._lock:
            return self._keys.pop(provider, None) is not None

    def clear_all(self) -> None:
        with self._lock:
            self._keys.clear()

    def has(self, provider: str) -> bool:
        with self._lock:
            return bool(self._keys.get(provider))

    def get(self, provider: str) -> str:
        """The raw key, for building a provider request. Never log this."""
        with self._lock:
            return self._keys.get(provider, "")

    def masked(self, provider: str) -> str:
        with self._lock:
            return mask(self._keys.get(provider, ""))

    def status(self) -> dict:
        """Safe summary for the UI: connected flag plus masked tail only."""
        with self._lock:
            return {
                provider: {
                    "connected": bool(self._keys.get(provider)),
                    "masked": mask(self._keys.get(provider, "")),
                }
                for provider in PROVIDERS
            }

    # Never let a raw key reach a log line, a traceback or a debugger.
    def __repr__(self) -> str:
        with self._lock:
            connected = sorted(p for p, v in self._keys.items() if v)
        return f"<KeyStore connected={connected} (values never shown)>"

    __str__ = __repr__


_store = _KeyStore()


def set_key(provider: str, api_key: str) -> str:
    return _store.set(provider, api_key)


def clear_key(provider: str) -> bool:
    return _store.clear(provider)


def clear_all() -> None:
    _store.clear_all()


def has_key(provider: str) -> bool:
    return _store.has(provider)


def get_key(provider: str) -> str:
    return _store.get(provider)


def masked(provider: str) -> str:
    return _store.masked(provider)


def status() -> dict:
    return _store.status()


def provider_for_tier(tier: str) -> str:
    return TIER_PROVIDER.get(tier, "")


def tier_has_user_key(tier: str) -> bool:
    provider = provider_for_tier(tier)
    return bool(provider) and has_key(provider)
