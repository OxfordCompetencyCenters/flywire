"""Static, per-environment endpoints published by Flywire.

Nothing in here is secret. Everything that differs between sandbox and production
lives in exactly one place so switching environments can never be half-done.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Environment(str, Enum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"

    @classmethod
    def parse(cls, value: str) -> "Environment":
        normalized = value.strip().lower()
        aliases = {"test": cls.SANDBOX, "demo": cls.SANDBOX, "prod": cls.PRODUCTION, "live": cls.PRODUCTION}
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError:
            allowed = ", ".join(e.value for e in cls)
            raise ValueError(f"Unknown Flywire environment {value!r}; expected one of: {allowed}") from None


@dataclass(frozen=True)
class EnvironmentProfile:
    environment: Environment
    api_base_url: str
    """Base URL for the Payments API, always ending with a slash."""
    sdk_script_url: str
    """Flywire JS SDK used by the browser to render the checkout element."""


_PROFILES: dict[Environment, EnvironmentProfile] = {
    Environment.SANDBOX: EnvironmentProfile(
        environment=Environment.SANDBOX,
        api_base_url="https://api-platform-sandbox.flywire.com/payments/v1/",
        sdk_script_url="https://artifacts.flywire.com/sdk/js/v0/sandbox.main.js",
    ),
    Environment.PRODUCTION: EnvironmentProfile(
        environment=Environment.PRODUCTION,
        api_base_url="https://api-platform.flywire.com/payments/v1/",
        sdk_script_url="https://artifacts.flywire.com/sdk/js/v0/main.js",
    ),
}


def profile_for(environment: Environment) -> EnvironmentProfile:
    return _PROFILES[environment]
