from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    user_agent: str = Field(
        default="GlobalCompanyUniverse/0.3 research@example.com",
        validation_alias=AliasChoices("GCU_USER_AGENT", "USER_AGENT"),
    )
    contact_email: str = Field(
        default="research@example.com",
        validation_alias=AliasChoices("GCU_CONTACT_EMAIL", "CONTACT_EMAIL"),
    )
    data_dir: Path = Field(default=Path("data"), validation_alias="GCU_DATA_DIR")
    database_path: Path = Field(
        default=Path("data/state/gcu.sqlite3"), validation_alias="GCU_DATABASE_PATH"
    )
    raw_dir: Path = Field(default=Path("data/raw"), validation_alias="GCU_RAW_DIR")
    http_timeout_seconds: float = Field(default=60.0, validation_alias="GCU_HTTP_TIMEOUT_SECONDS")
    http_max_retries: int = Field(default=4, validation_alias="GCU_HTTP_MAX_RETRIES")
    default_requests_per_second: float = Field(
        default=1.0, validation_alias="GCU_DEFAULT_REQUESTS_PER_SECOND"
    )
    sec_requests_per_second: float = Field(
        default=8.0, validation_alias="GCU_SEC_REQUESTS_PER_SECOND"
    )
    verify_tls: bool = Field(default=True, validation_alias="GCU_VERIFY_TLS")
    proxy_pool_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GCU_PROXY_POOL_URL", "VALUECHAIN_PROXY_POOL_URL"),
    )

    edinet_api_key: str | None = Field(default=None, validation_alias="EDINET_API_KEY")
    opendart_api_key: str | None = Field(default=None, validation_alias="OPENDART_API_KEY")
    companies_house_api_key: str | None = Field(
        default=None, validation_alias="COMPANIES_HOUSE_API_KEY"
    )

    @field_validator(
        "http_timeout_seconds", "default_requests_per_second", "sec_requests_per_second"
    )
    @classmethod
    def positive_number(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("value must be positive")
        return value

    @field_validator("http_max_retries")
    @classmethod
    def nonnegative_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("GCU_HTTP_MAX_RETRIES must be non-negative")
        return value

    def credential(self, environment_variable: str | None) -> str | None:
        if not environment_variable:
            return None
        mapping = {
            "EDINET_API_KEY": self.edinet_api_key,
            "OPENDART_API_KEY": self.opendart_api_key,
            "COMPANIES_HOUSE_API_KEY": self.companies_house_api_key,
        }
        return mapping.get(environment_variable)

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
