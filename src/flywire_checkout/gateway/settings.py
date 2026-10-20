"""Gateway runtime settings (environment variables, no secrets in files)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from ..exceptions import ConfigurationError


class GatewaySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    public_url: str
    """Public base URL of this gateway, e.g. https://payments.example.ac.uk"""
    secret: SecretStr
    """Signs payment-link tokens. Rotating it invalidates all outstanding payment links."""
    sites_file: str = "sites.toml"
    db_path: str = "data/gateway.db"
    checkout_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)

    @field_validator("public_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        parts = urlsplit(value)
        local = parts.hostname in ("localhost", "127.0.0.1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ValueError("GATEWAY_PUBLIC_URL must be https:// (http:// only for localhost)")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("GATEWAY_PUBLIC_URL must be scheme://host[:port] only")
        return value.rstrip("/")

    @field_validator("secret")
    @classmethod
    def _secret_strength(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("GATEWAY_SECRET must be at least 32 characters")
        return value

    @property
    def is_https(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "GatewaySettings":
        env = os.environ if environ is None else environ
        raw = {
            "public_url": env.get("GATEWAY_PUBLIC_URL"),
            "secret": env.get("GATEWAY_SECRET"),
            "sites_file": env.get("GATEWAY_SITES_FILE"),
            "db_path": env.get("GATEWAY_DB_PATH"),
            "checkout_ttl_hours": env.get("GATEWAY_CHECKOUT_TTL_HOURS"),
        }
        missing = [k for k in ("public_url", "secret") if not raw[k]]
        if missing:
            raise ConfigurationError("Missing " + ", ".join(f"GATEWAY_{k.upper()}" for k in missing))
        try:
            return cls(**{k: v for k, v in raw.items() if v})
        except ValidationError as exc:
            raise ConfigurationError(f"Invalid gateway settings: {exc}") from None
