
import json
import os
from dataclasses import dataclass
from pathlib import Path

TIERS = ("small", "medium", "big")


def _load_env_file(path: Path) -> None:
    """Tiny .env loader (no extra dependency). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(Path(__file__).with_name(".env"))

# Comma-separated list of browser origins allowed to call the API.
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5500,http://127.0.0.1:5500")
ALLOWED_ORIGINS = [o.strip() for o in FRONTEND_ORIGIN.split(",") if o.strip()]
DATABASE_PATH = os.getenv("DATABASE_PATH", "greenprompt.db")


@dataclass(frozen=True)
class TierConfig:
    tier: str
    base_url: str
    api_key: str
    model: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


# ---------- Model catalog (backend/model_catalog.json) ----------
# Maps real models from several providers onto OUR size labels. The file holds
# no credentials: each entry names the ENVIRONMENT VARIABLE that carries its key.
CATALOG_PATH = Path(__file__).with_name("model_catalog.json")
_CATALOG_FIELDS = ("id", "provider", "label", "tier", "key_env", "base_url", "enabled", "note")


def load_catalog(path: Path | None = None) -> list:
    """Catalog entries, or [] if the file is missing or unreadable.

    Never raises: a broken catalog must not take the backend down, because the
    per-tier environment variables below still work on their own.
    """
    target = CATALOG_PATH if path is None else path
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    out = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        if entry.get("tier") not in TIERS:
            continue
        if not all(isinstance(entry.get(key), str) for key in ("id", "provider", "label", "key_env", "base_url")):
            continue
        out.append({key: entry.get(key) for key in _CATALOG_FIELDS})
    return out


def _key_for(entry: dict) -> str:
    """The value of the entry's key_env variable. Never logged or returned."""
    return os.getenv(entry.get("key_env") or "", "").strip()


def catalog_entry_for_tier(tier: str, catalog: list | None = None):
    """First enabled entry for this tier whose key_env variable is set."""
    for entry in load_catalog() if catalog is None else catalog:
        if entry.get("tier") == tier and entry.get("enabled") and _key_for(entry):
            return entry
    return None


def get_tier_config(tier: str) -> TierConfig:
    """A tier's provider settings, read at call time (so tests can change them).

    Precedence:
      1. An explicit, complete {TIER}_BASE_URL + _API_KEY + _MODEL trio. These
         are the operator's deliberate per-tier override and always win.
      2. The first enabled catalog entry whose key_env variable is set.
      3. Whatever partial {TIER}_* values exist (so an incomplete tier still
         reports CONFIG_MISSING exactly as before).

    Explicit variables win over the catalog on purpose: a provider-wide key such
    as GROQ_API_KEY may be present in the machine environment and differ from
    the per-tier key, and silently preferring it would swap credentials under a
    working deployment.
    """
    prefix = tier.upper()
    env = TierConfig(
        tier=tier,
        base_url=os.getenv(f"{prefix}_BASE_URL", "").strip().rstrip("/"),
        api_key=os.getenv(f"{prefix}_API_KEY", "").strip(),
        model=os.getenv(f"{prefix}_MODEL", "").strip(),
    )
    if env.configured:
        return env

    # PROPOSED: a key the user pasted into the website, held in memory only.
    user = user_key_config(tier)
    if user is not None and user.configured:
        return user

    entry = catalog_entry_for_tier(tier)
    if entry is not None:
        return TierConfig(
            tier=tier,
            base_url=(entry.get("base_url") or "").strip().rstrip("/"),
            api_key=_key_for(entry),
            model=(entry.get("id") or "").strip(),
        )
    return env


def catalog_entry_for_provider_tier(provider: str, tier: str):
    """The catalog row for one provider at one tier, enabled or not.

    PROPOSED (bring your own key): a user-supplied key activates the row for
    that provider even when the catalog ships it disabled, because "enabled"
    only describes what the demo account can reach.
    """
    for entry in load_catalog():
        if entry.get("provider") == provider and entry.get("tier") == tier:
            return entry
    return None


def user_key_config(tier: str):
    """TierConfig built from a runtime user key, or None when there is none."""
    from backend import runtime_keys  # local import keeps config import-light

    provider = runtime_keys.provider_for_tier(tier)
    if not provider or not runtime_keys.has_key(provider):
        return None
    entry = catalog_entry_for_provider_tier(runtime_keys.CATALOG_PROVIDER[provider], tier)
    if entry is None:
        return None
    return TierConfig(
        tier=tier,
        base_url=(entry.get("base_url") or "").strip().rstrip("/"),
        api_key=runtime_keys.get_key(provider),
        model=(entry.get("id") or "").strip(),
    )


def tier_source(tier: str) -> str:
    """Where this tier's settings come from: "env", "catalog", or "none".

    Decided by which layer actually won, not by comparing model ids -- an
    override and a catalog entry naming the same model are indistinguishable
    that way.
    """
    prefix = tier.upper()
    if all(os.getenv(f"{prefix}_{part}", "").strip() for part in ("BASE_URL", "API_KEY", "MODEL")):
        return "env"
    if user_key_config(tier) is not None:
        return "your key"
    return "demo (Groq)" if catalog_entry_for_tier(tier) is not None else "none"


def catalog_status() -> list:
    """Catalog rows for GET /models. Never includes a key, only whether one is set."""
    rows = []
    for entry in load_catalog():
        rows.append({
            "id": entry.get("id"),
            "label": entry.get("label"),
            "provider": entry.get("provider"),
            "tier": entry.get("tier"),
            "status": "live" if (entry.get("enabled") and _key_for(entry)) else "not_connected",
            "enabled": bool(entry.get("enabled")),
            "key_env": entry.get("key_env"),
            "note": entry.get("note"),
        })
    return rows


def provider_timeout_seconds() -> float:
    try:
        return float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "30"))
    except ValueError:
        return 30.0


def dev_stubs_enabled() -> bool:
    """Isolated development stubs for unfinished team components (off by default)."""
    return os.getenv("GREENPROMPT_DEV_STUBS", "").strip().lower() in ("1", "true", "yes")


def provider_status() -> dict:
    """Which tiers have settings. Never includes keys."""
    out = {}
    for tier in TIERS:
        cfg = get_tier_config(tier)
        out[tier] = {"configured": cfg.configured, "model": cfg.model or None}
    return out


# ---------- Difficulty classifier (hybrid: rules first, model only if needed) ----------
# Separate from PROVIDER_TIMEOUT_SECONDS on purpose: the classifier is a small,
# optional helper call and must never hold up the answer for long.
CLASSIFIER_TIER_DEFAULT = "small"
CLASSIFIER_TIMEOUT_DEFAULT = 5.0


def classifier_tier() -> str:
    """Which tier runs the model classifier. Read at call time so tests can change it."""
    tier = os.getenv("CLASSIFIER_TIER", CLASSIFIER_TIER_DEFAULT).strip().lower()
    return tier if tier in TIERS else CLASSIFIER_TIER_DEFAULT


def classifier_timeout_seconds() -> float:
    """Total deadline for one classifier request. There is no automatic retry."""
    try:
        value = float(os.getenv("CLASSIFIER_TIMEOUT_SECONDS", str(CLASSIFIER_TIMEOUT_DEFAULT)))
    except ValueError:
        return CLASSIFIER_TIMEOUT_DEFAULT
    return value if value > 0 else CLASSIFIER_TIMEOUT_DEFAULT
