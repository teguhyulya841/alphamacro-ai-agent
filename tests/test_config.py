import pytest

from alphamacro.config import BotSettings, Settings
from alphamacro.exceptions import ConfigurationError

VALID_TOKEN = "123456789:" + "A" * 35


def test_missing_keys_reported(clean_env):
    with pytest.raises(ConfigurationError, match="GOOGLE_API_KEY"):
        Settings.from_env(load_env=False)


def test_defaults_and_overrides(clean_env):
    clean_env.setenv("GOOGLE_API_KEY", "gkey")
    clean_env.setenv("TAVILY_API_KEY", "tkey")
    clean_env.setenv("CACHE_TTL_SECONDS", "0")
    s = Settings.from_env(load_env=False)
    assert s.cache_ttl == 0
    assert s.log_level == "INFO"
    assert s.coingecko_api_key is None


@pytest.mark.parametrize(
    ("name", "value"),
    [("LLM_TIMEOUT", "abc"), ("LLM_TIMEOUT", "0"), ("LOG_LEVEL", "LOUD"), ("LOG_FORMAT", "xml")],
)
def test_invalid_values_rejected(clean_env, name, value):
    clean_env.setenv("GOOGLE_API_KEY", "gkey")
    clean_env.setenv("TAVILY_API_KEY", "tkey")
    clean_env.setenv(name, value)
    with pytest.raises(ConfigurationError):
        Settings.from_env(load_env=False)


def test_secrets_not_in_repr(settings):
    assert "gggg" not in repr(settings)
    assert settings.google_api_key.get_secret_value() in settings.secrets()


def test_bot_settings_valid(clean_env):
    clean_env.setenv("TELEGRAM_TOKEN", VALID_TOKEN)
    clean_env.setenv("TELEGRAM_ALLOWED_USER_IDS", "1, 2,3")
    b = BotSettings.from_env(load_env=False)
    assert b.allowed_user_ids == frozenset({1, 2, 3})
    assert VALID_TOKEN not in repr(b)


def test_bot_token_format_validated_without_echo(clean_env):
    clean_env.setenv("TELEGRAM_TOKEN", "not-a-token")
    with pytest.raises(ConfigurationError) as info:
        BotSettings.from_env(load_env=False)
    assert "not-a-token" not in str(info.value)


def test_bot_allowed_ids_must_be_ints(clean_env):
    clean_env.setenv("TELEGRAM_TOKEN", VALID_TOKEN)
    clean_env.setenv("TELEGRAM_ALLOWED_USER_IDS", "abc")
    with pytest.raises(ConfigurationError):
        BotSettings.from_env(load_env=False)
