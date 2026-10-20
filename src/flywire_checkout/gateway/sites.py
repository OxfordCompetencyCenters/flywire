"""Site registry — the one place where websites are added.

``sites.toml`` lists every website that may take payments through the gateway.
It contains no secrets: each site names the *environment variables* that hold its
API key and webhook secret, so the file can be committed and reviewed.

    [sites.website-1]
    name = "Website 1"
    origins = ["https://web1.example.ac.uk"]
    return_url = "https://web1.example.ac.uk/payment/return"
    webhook_url = "https://web1.example.ac.uk/api/payment-events"
    api_key_env = "SITE_WEBSITE_1_API_KEY"
    webhook_secret_env = "SITE_WEBSITE_1_WEBHOOK_SECRET"
"""

from __future__ import annotations

import hashlib
import hmac
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from ..exceptions import ConfigurationError

_SITE_ID = r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$"
MIN_SECRET_LENGTH = 32


def _https_url(value: str, *, allow_localhost: bool) -> str:
    parts = urlsplit(value)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if parts.scheme != "https" and not (allow_localhost and local and parts.scheme == "http"):
        raise ValueError(f"{value!r} must be an https:// URL")
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError(f"{value!r} is not an allowed URL")
    return value


class SiteConfig(BaseModel):
    """One website. Everything a site needs is declared here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_SITE_ID)
    name: str = Field(min_length=1, max_length=100)
    origins: list[str] = Field(min_length=1)
    """Browser origins allowed to embed the payment page (iframe) — e.g. https://web1.example.ac.uk"""
    return_url: str
    """Where the payer is sent after submitting the form (redirect mode)."""
    cancel_url: str | None = None
    """Where "back to site" links go. Defaults to return_url."""
    webhook_url: str | None = None
    """Where payment events are POSTed (signed). Optional — sites can poll instead."""
    recipient_id: str | None = Field(default=None, pattern=r"^([A-Za-z]{3}|[A-Za-z][A-Za-z0-9]{4})$")
    """Flywire portal for this site. Defaults to FLYWIRE_<ENV>_RECIPIENT_ID."""
    required_fields: list[str] = Field(default_factory=list)
    """Recipient fields the portal requires (e.g. student_email); checked before calling Flywire."""
    session_types: list[str] = Field(default_factory=lambda: ["one_off"])
    max_amount: int | None = Field(default=None, gt=0)
    """Optional safety limit in currency subunits."""
    locale: str = Field(default="en", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")

    api_key: SecretStr
    webhook_secret: SecretStr | None = None
    allow_localhost: bool = False
    """Permit http://localhost URLs/origins. For local development only."""

    @model_validator(mode="after")
    def _validate_urls(self) -> "SiteConfig":
        local = self.allow_localhost
        for origin in self.origins:
            _https_url(origin, allow_localhost=local)
            parts = urlsplit(origin)
            if parts.path not in ("", "/") or parts.query:
                raise ValueError(f"origin {origin!r} must be scheme://host[:port] only")
        _https_url(self.return_url, allow_localhost=local)
        if self.cancel_url:
            _https_url(self.cancel_url, allow_localhost=local)
        if self.webhook_url:
            _https_url(self.webhook_url, allow_localhost=local)
            if self.webhook_secret is None:
                raise ValueError("webhook_url requires webhook_secret_env")
        unknown = set(self.session_types) - {"one_off", "recurring"}
        if unknown:
            raise ValueError(f"unsupported session_types: {sorted(unknown)} (gateway supports one_off, recurring)")
        return self

    @field_validator("origins")
    @classmethod
    def _normalise_origins(cls, values: list[str]) -> list[str]:
        return [v.rstrip("/") for v in values]

    @property
    def effective_cancel_url(self) -> str:
        return self.cancel_url or self.return_url

    def __repr__(self) -> str:
        return f"SiteConfig(id={self.id!r}, name={self.name!r})"


class SiteRegistry:
    def __init__(self, sites: list[SiteConfig]) -> None:
        if not sites:
            raise ConfigurationError("No sites configured")
        self._by_id = {s.id: s for s in sites}
        if len(self._by_id) != len(sites):
            raise ConfigurationError("Duplicate site ids")
        digests = [hashlib.sha256(s.api_key.get_secret_value().encode()).digest() for s in sites]
        if len(set(digests)) != len(digests):
            raise ConfigurationError("Each site must have its own API key")
        self._key_digests = list(zip(digests, sites, strict=True))

    def __iter__(self):
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def get(self, site_id: str) -> SiteConfig | None:
        return self._by_id.get(site_id)

    def authenticate(self, presented_key: str | None) -> SiteConfig | None:
        """Find the site owning this API key. Constant-time per comparison; checks every site."""
        if not presented_key:
            return None
        presented = hashlib.sha256(presented_key.encode()).digest()
        match = None
        for digest, site in self._key_digests:
            if hmac.compare_digest(digest, presented):
                match = site
        return match

    @classmethod
    def load(cls, path: str | os.PathLike, environ: Mapping[str, str] | None = None) -> "SiteRegistry":
        env = os.environ if environ is None else environ
        try:
            raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigurationError(f"Sites file not found: {path}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from None

        sites_raw = raw.get("sites")
        if not isinstance(sites_raw, dict) or not sites_raw:
            raise ConfigurationError(f"{path} must define at least one [sites.<id>] table")

        sites = []
        for site_id, block in sites_raw.items():
            if not isinstance(block, dict):
                raise ConfigurationError(f"[sites.{site_id}] must be a table")
            block = dict(block)
            key_env = block.pop("api_key_env", None)
            secret_env = block.pop("webhook_secret_env", None)
            if not key_env:
                raise ConfigurationError(f"[sites.{site_id}] needs api_key_env")
            api_key = env.get(key_env, "")
            if len(api_key) < MIN_SECRET_LENGTH:
                raise ConfigurationError(
                    f"[sites.{site_id}] environment variable {key_env} must hold a key of at least "
                    f"{MIN_SECRET_LENGTH} characters (generate one with: python -m flywire_checkout.gateway new-key)"
                )
            webhook_secret = None
            if secret_env:
                webhook_secret = env.get(secret_env, "")
                if len(webhook_secret) < MIN_SECRET_LENGTH:
                    raise ConfigurationError(
                        f"[sites.{site_id}] environment variable {secret_env} must hold a secret of at least "
                        f"{MIN_SECRET_LENGTH} characters"
                    )
            try:
                sites.append(SiteConfig(id=site_id, api_key=api_key, webhook_secret=webhook_secret, **block))
            except ValidationError as exc:
                # Pydantic never renders SecretStr values, so this is safe to surface.
                raise ConfigurationError(f"[sites.{site_id}] is invalid: {exc}") from None
        return cls(sites)
