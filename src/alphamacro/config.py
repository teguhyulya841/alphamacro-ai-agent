"""Typed configuration loaded from environment variables via pydantic-settings."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, Self

from dotenv import load_dotenv
from pydantic import (
    AliasChoices,
    BeforeValidator,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from alphamacro.exceptions import ConfigurationError
from alphamacro.models import DegradedMode, StateBackend

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_TELEGRAM_TOKEN_RE = re.compile(r"^\d+:[\w-]{20,}$")
_KEY_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSET: Any = object()


def _empty_to_none(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _raise_config_error(exc: ValidationError, env_names: dict[str, str]) -> NoReturn:
    """Convert Pydantic ValidationError into ConfigurationError without echoing inputs."""
    missing: list[str] = []
    details: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        if err.get("type") == "missing" and loc in env_names:
            missing.append(env_names[loc])
        else:
            label = env_names.get(loc, loc)
            details.append(f"{label}: {err.get('msg', 'invalid value')}")
    if missing:
        raise ConfigurationError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values."
        ) from exc
    raise ConfigurationError("; ".join(details)) from exc


def _parse_allowed_ids(value: Any) -> frozenset[int]:
    if value is None or value == "":
        return frozenset()
    if isinstance(value, frozenset | set | list | tuple):
        return frozenset(int(item) for item in value)
    if isinstance(value, str):
        allowed: set[int] = set()
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                allowed.add(int(part))
            except ValueError as exc:
                raise ValueError(
                    "TELEGRAM_ALLOWED_USER_IDS must be comma-separated integers."
                ) from exc
        return frozenset(allowed)
    raise ValueError("Invalid TELEGRAM_ALLOWED_USER_IDS format.")


def _parse_api_key_hashes(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k).strip(): str(v).strip().lower() for k, v in value.items()}
    if not isinstance(value, str) or not value.strip():
        raise ValueError("API_KEY_HASHES must not be empty.")
    hashes: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, digest = item.partition(":")
        clean_name = name.strip()
        clean_digest = digest.strip().lower()
        if not sep or not clean_name or not _KEY_HASH_RE.match(clean_digest):
            raise ValueError(
                "API_KEY_HASHES must look like 'name:sha256hex[,name2:sha256hex]'."
            )
        hashes[clean_name] = clean_digest
    if not hashes:
        raise ValueError("API_KEY_HASHES contains no keys.")
    return hashes


class Settings(BaseSettings):
    """Core settings. Secret fields are wrapped in SecretStr to prevent log leaks."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    google_api_key: SecretStr
    tavily_api_key: SecretStr
    gemini_model: str = DEFAULT_GEMINI_MODEL
    coingecko_api_key: Annotated[SecretStr | None, BeforeValidator(_empty_to_none)] = (
        None
    )
    llm_timeout: int = Field(default=60, ge=1)
    llm_max_retries: int = Field(default=2, ge=0)
    http_timeout: int = Field(default=15, ge=1)
    cache_ttl: int = Field(
        default=60,
        ge=0,
        validation_alias=AliasChoices("cache_ttl", "CACHE_TTL_SECONDS"),
    )
    max_query_chars: int = Field(default=1500, ge=10)
    recursion_limit: int = Field(
        default=25,
        ge=3,
        validation_alias=AliasChoices("recursion_limit", "AGENT_RECURSION_LIMIT"),
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    grounding_min_score: float = Field(default=0.8, ge=0.0, le=1.0)
    audit_log_path: Annotated[str | None, BeforeValidator(_empty_to_none)] = None
    audit_store_query_text: bool = False
    audit_salt: Annotated[SecretStr | None, BeforeValidator(_empty_to_none)] = None

    def __init__(
        self,
        google_api_key: str | SecretStr = _UNSET,
        tavily_api_key: str | SecretStr = _UNSET,
        **data: Any,
    ) -> None:
        if google_api_key is not _UNSET:
            data.setdefault("google_api_key", google_api_key)
        if tavily_api_key is not _UNSET:
            data.setdefault("tavily_api_key", tavily_api_key)
        super().__init__(**data)

    @field_validator("google_api_key", "tavily_api_key", mode="before")
    @classmethod
    def _require_non_empty_secret(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            raw = value.get_secret_value().strip()
            if not raw:
                raise ValueError("Secret value must not be empty.")
            return SecretStr(raw)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                raise ValueError("Secret value must not be empty.")
            return raw
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> str:
        raw = str(value).strip().upper() if value is not None else "INFO"
        if raw not in _VALID_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}.")
        return raw

    @field_validator("log_format", mode="before")
    @classmethod
    def _normalize_log_format(cls, value: Any) -> str:
        raw = str(value).strip().lower() if value is not None else "text"
        if raw not in {"text", "json"}:
            raise ValueError("LOG_FORMAT must be 'text' or 'json'.")
        return raw

    @model_validator(mode="after")
    def _validate_audit_salt(self) -> Self:
        if self.audit_log_path and (
            self.audit_salt is None or not self.audit_salt.get_secret_value().strip()
        ):
            raise ValueError("AUDIT_SALT is required when AUDIT_LOG_PATH is set.")
        return self

    @classmethod
    def from_env(cls, *, load_env: bool = True) -> Settings:
        if load_env:
            load_dotenv()
        try:
            return cls()
        except ValidationError as exc:
            _raise_config_error(
                exc,
                {
                    "google_api_key": "GOOGLE_API_KEY",
                    "tavily_api_key": "TAVILY_API_KEY",
                },
            )

    def secrets(self) -> tuple[str, ...]:
        """Plaintext secret values registered for log redaction filters."""
        candidates = (
            self.google_api_key.get_secret_value(),
            self.tavily_api_key.get_secret_value(),
            self.coingecko_api_key.get_secret_value()
            if self.coingecko_api_key
            else None,
            self.audit_salt.get_secret_value() if self.audit_salt else None,
        )
        return tuple(s for s in candidates if s)


class BotSettings(BaseSettings):
    """Telegram bot settings."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    telegram_token: SecretStr
    allowed_user_ids: Annotated[
        frozenset[int],
        NoDecode,
        BeforeValidator(_parse_allowed_ids),
    ] = Field(
        default_factory=frozenset,
        validation_alias=AliasChoices("allowed_user_ids", "TELEGRAM_ALLOWED_USER_IDS"),
    )
    rate_limit_per_minute: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices(
            "rate_limit_per_minute", "BOT_RATE_LIMIT_PER_MINUTE"
        ),
    )
    max_concurrent_jobs: int = Field(
        default=4,
        ge=1,
        validation_alias=AliasChoices("max_concurrent_jobs", "BOT_MAX_CONCURRENT_JOBS"),
    )

    def __init__(
        self,
        telegram_token: str | SecretStr = _UNSET,
        allowed_user_ids: frozenset[int] | set[int] | list[int] | str = _UNSET,
        rate_limit_per_minute: int = _UNSET,
        max_concurrent_jobs: int = _UNSET,
        **data: Any,
    ) -> None:
        if telegram_token is not _UNSET:
            data.setdefault("telegram_token", telegram_token)
        if allowed_user_ids is not _UNSET:
            data.setdefault("allowed_user_ids", allowed_user_ids)
        if rate_limit_per_minute is not _UNSET:
            data.setdefault("rate_limit_per_minute", rate_limit_per_minute)
        if max_concurrent_jobs is not _UNSET:
            data.setdefault("max_concurrent_jobs", max_concurrent_jobs)
        super().__init__(**data)

    @classmethod
    def from_env(cls, *, load_env: bool = True) -> BotSettings:
        if load_env:
            load_dotenv()
        try:
            cfg = cls()
        except ValidationError as exc:
            _raise_config_error(exc, {"telegram_token": "TELEGRAM_TOKEN"})
        raw_token = cfg.telegram_token.get_secret_value().strip()
        if not _TELEGRAM_TOKEN_RE.match(raw_token):
            raise ConfigurationError("TELEGRAM_TOKEN has an invalid format.")
        return cfg


class ApiSettings(BaseSettings):
    """REST API settings. Keys are stored only as SHA-256 hashes."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    api_key_hashes: Annotated[
        dict[str, str],
        NoDecode,
        BeforeValidator(_parse_api_key_hashes),
        Field(repr=False),
    ]
    rate_limit_per_minute: int = Field(
        default=30,
        ge=1,
        validation_alias=AliasChoices(
            "rate_limit_per_minute", "API_RATE_LIMIT_PER_MINUTE"
        ),
    )
    max_concurrent_requests: int = Field(
        default=4,
        ge=1,
        validation_alias=AliasChoices("max_concurrent_requests", "API_MAX_CONCURRENT"),
    )
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("host", "API_HOST"),
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("port", "API_PORT"),
    )

    def __init__(
        self,
        api_key_hashes: dict[str, str] | str = _UNSET,
        rate_limit_per_minute: int = _UNSET,
        max_concurrent_requests: int = _UNSET,
        host: str = _UNSET,
        port: int = _UNSET,
        **data: Any,
    ) -> None:
        if api_key_hashes is not _UNSET:
            data.setdefault("api_key_hashes", api_key_hashes)
        if rate_limit_per_minute is not _UNSET:
            data.setdefault("rate_limit_per_minute", rate_limit_per_minute)
        if max_concurrent_requests is not _UNSET:
            data.setdefault("max_concurrent_requests", max_concurrent_requests)
        if host is not _UNSET:
            data.setdefault("host", host)
        if port is not _UNSET:
            data.setdefault("port", port)
        super().__init__(**data)

    @classmethod
    def from_env(cls, *, load_env: bool = True) -> ApiSettings:
        if load_env:
            load_dotenv()
        try:
            return cls()
        except ValidationError as exc:
            _raise_config_error(exc, {"api_key_hashes": "API_KEY_HASHES"})


class StateSettings(BaseSettings):
    """Persistent cache, rate limiter, and lock store configuration (M1)."""

    model_config = SettingsConfigDict(
        env_prefix="STATE_",
        extra="ignore",
        populate_by_name=True,
    )

    backend: StateBackend = StateBackend.SQLITE
    sqlite_path: Path = Path("data/alphamacro.sqlite3")
    redis_url: SecretStr | None = None
    key_prefix: str = "amc:v1:"
    degraded_mode: DegradedMode = DegradedMode.FAIL_OPEN

    @model_validator(mode="after")
    def _backend_requirements(self) -> Self:
        if self.backend is StateBackend.REDIS and self.redis_url is None:
            raise ValueError("STATE_REDIS_URL is required when STATE_BACKEND=redis")
        return self

    @classmethod
    def from_env(cls, *, load_env: bool = True) -> StateSettings:
        if load_env:
            load_dotenv()
        try:
            return cls()
        except ValidationError as exc:
            _raise_config_error(exc, {"redis_url": "STATE_REDIS_URL"})
