import pytest

from flywire_checkout import ConfigurationError, Environment, FlywireConfig


def test_defaults_to_sandbox():
    cfg = FlywireConfig.from_env({"FLYWIRE_API_KEY": "k"})
    assert cfg.environment is Environment.SANDBOX
    assert cfg.api_base_url == "https://api-platform-sandbox.flywire.com/payments/v1/"
    assert cfg.profile.sdk_script_url.endswith("/sandbox.main.js")


def test_switching_environment_is_one_variable():
    env = {
        "FLYWIRE_SANDBOX_API_KEY": "sandbox-key",
        "FLYWIRE_PRODUCTION_API_KEY": "prod-key",
        "FLYWIRE_SANDBOX_RECIPIENT_ID": "TQQ",
        "FLYWIRE_PRODUCTION_RECIPIENT_ID": "ABC",
    }
    sandbox = FlywireConfig.from_env({**env, "FLYWIRE_ENVIRONMENT": "sandbox"})
    prod = FlywireConfig.from_env({**env, "FLYWIRE_ENVIRONMENT": "production"})

    assert sandbox.api_key.get_secret_value() == "sandbox-key"
    assert sandbox.recipient_id == "TQQ"
    assert prod.api_key.get_secret_value() == "prod-key"
    assert prod.recipient_id == "ABC"
    assert prod.api_base_url == "https://api-platform.flywire.com/payments/v1/"
    assert prod.profile.sdk_script_url.endswith("/v0/main.js")


@pytest.mark.parametrize("alias, expected", [("test", Environment.SANDBOX), ("PROD", Environment.PRODUCTION)])
def test_environment_aliases(alias, expected):
    assert FlywireConfig.from_env({"FLYWIRE_ENVIRONMENT": alias, "FLYWIRE_API_KEY": "k"}).environment is expected


def test_missing_key_names_the_variable():
    with pytest.raises(ConfigurationError, match="FLYWIRE_PRODUCTION_API_KEY"):
        FlywireConfig.from_env({"FLYWIRE_ENVIRONMENT": "production", "FLYWIRE_SANDBOX_API_KEY": "k"})


def test_unknown_environment_rejected():
    with pytest.raises(ConfigurationError):
        FlywireConfig.from_env({"FLYWIRE_ENVIRONMENT": "staging", "FLYWIRE_API_KEY": "k"})


def test_secrets_never_in_repr():
    cfg = FlywireConfig(api_key="super-secret-key", shared_secret="super-secret-hmac")
    assert "super-secret" not in repr(cfg)
    assert "super-secret" not in str(cfg.model_dump())


def test_notifications_url_must_be_https():
    with pytest.raises(ConfigurationError):
        FlywireConfig.from_env({"FLYWIRE_API_KEY": "k", "FLYWIRE_NOTIFICATIONS_URL": "http://example.com/hook"})


def test_base_url_override_forbidden_in_production():
    with pytest.raises(ValueError):
        FlywireConfig(environment="production", api_key="k", api_base_url_override="https://proxy.example.com/")
