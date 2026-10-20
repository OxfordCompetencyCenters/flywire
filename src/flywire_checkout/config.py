"""Runtime configuration.

Secrets are only ever read from the environment (or an explicit constructor call),
held as :class:`pydantic.SecretStr`, and never appear in ``repr``/logs.

Switching between sandbox and production is a single variable::

    FLYWIRE_ENVIRONMENT=sandbox   # or production

Credentials are looked up as ``FLYWIRE_<ENV>_<NAME>`` first and ``FLYWIRE_<NAME>``
second, so both credential sets can live side by side.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .environments import Environment, EnvironmentProfile, profile_for
from .exceptions import ConfigurationError

_ENV_PREFIX = "FLYWIRE_"


class FlywireConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Environment = Environment.SANDBOX
    api_key: SecretStr
    shared_secret: SecretStr | None = None
    """Used to verify X-Flywire-Digest on webhooks. Required to accept notifications."""
    frontend_key: SecretStr | None = None
    """Publishable key for the browser SDK. Not highly sensitive, but kept out of logs anyway."""
    recipient_id: str | None = Field(default=None, pattern=r"^([A-Za-z]{3}|[A-Za-z][A-Za-z0-9]{4})$")
    notifications_url: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_retries: int = Field(default=2, ge=0, le=5)
    """Retries for transient failures on safe (non-mutating) requests only."""
    api_base_url_override: str | None = None
    """For tests/proxies only. Must be HTTPS."""

    @field_validator("api_key")
    @classmethod
    def _api_key_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must not be empty")
        return value

    @field_validator("notifications_url", "api_base_url_override")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("must be an absolute https:// URL")
        if parts.username or parts.password:
            raise ValueError("must not contain credentials")
        return value

    @model_validator(mode="after")
    def _no_override_in_production(self) -> "FlywireConfig":
        if self.environment is Environment.PRODUCTION and self.api_base_url_override:
            raise ValueError("api_base_url_override is not allowed in production")
        return self

    @property
    def profile(self) -> EnvironmentProfile:
        return profile_for(self.environment)

    @property
    def api_base_url(self) -> str:
        base = self.api_base_url_override or self.profile.api_base_url
        return base if base.endswith("/") else base + "/"

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FlywireConfig":
        env = os.environ if environ is None else environ
        try:
            environment = Environment.parse(env.get(f"{_ENV_PREFIX}ENVIRONMENT", Environment.SANDBOX.value))
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from None

        def lookup(name: str) -> str | None:
            scoped = env.get(f"{_ENV_PREFIX}{environment.name}_{name}")
            value = scoped if scoped not in (None, "") else env.get(f"{_ENV_PREFIX}{name}")
            return value.strip() if value and value.strip() else None

        api_key = lookup("API_KEY")
        if not api_key:
            raise ConfigurationError(
                f"No API key for the {environment.value} environment: set "
                f"{_ENV_PREFIX}{environment.name}_API_KEY (or {_ENV_PREFIX}API_KEY)."
            )

        raw = {
            "environment": environment,
            "api_key": api_key,
            "shared_secret": lookup("SHARED_SECRET"),
            "frontend_key": lookup("FRONTEND_KEY"),
            "recipient_id": lookup("RECIPIENT_ID"),
            "notifications_url": lookup("NOTIFICATIONS_URL"),
            "timeout_seconds": lookup("TIMEOUT_SECONDS"),
            "max_retries": lookup("MAX_RETRIES"),
            "api_base_url_override": lookup("API_BASE_URL_OVERRIDE"),
        }
        try:
            return cls(**{k: v for k, v in raw.items() if v is not None})
        except ValueError as exc:
            # pydantic's message never includes SecretStr values.
            raise ConfigurationError(f"Invalid Flywire configuration: {exc}") from None
