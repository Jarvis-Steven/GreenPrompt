"""Owner: backend member. Environment configuration.

Real secrets live only in backend/.env (ignored by git) or in the process
environment. Nothing in this file contains a credential.
"""
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


def get_tier_config(tier: str) -> TierConfig:
    """Read a tier's provider settings at call time (so tests can change them)."""
    prefix = tier.upper()
    return TierConfig(
        tier=tier,
        base_url=os.getenv(f"{prefix}_BASE_URL", "").strip().rstrip("/"),
        api_key=os.getenv(f"{prefix}_API_KEY", "").strip(),
        model=os.getenv(f"{prefix}_MODEL", "").strip(),
    )


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
