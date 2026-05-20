from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: str | list[str] | None) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    library_path: Path = Field(validation_alias="LIBRARY_PATH")
    data_path: Path = Field(default=Path("./data"), validation_alias="DATA_PATH")
    puid: int = Field(default=1000, validation_alias="PUID")
    pgid: int = Field(default=1000, validation_alias="PGID")
    tz: str = Field(default="Europe/London", validation_alias="TZ")

    port: int = Field(default=8084, validation_alias="CALIBRE_WEB_CLI_PORT")
    password: str | None = Field(default=None, validation_alias="CALIBRE_WEB_CLI_PASSWORD")
    metadata_sources: Annotated[list[str], NoDecode] = Field(
        default=["Amazon", "Google"], validation_alias="CALIBRE_WEB_CLI_METADATA_SOURCES"
    )
    device_format_order: Annotated[list[str], NoDecode] = Field(
        default=["EPUB", "AZW3", "MOBI", "PDF"],
        validation_alias="CALIBRE_WEB_CLI_DEVICE_FORMAT_ORDER",
    )
    page_size: int = Field(default=48, validation_alias="CALIBRE_WEB_CLI_PAGE_SIZE")
    mtp_usb_ids: Annotated[list[str], NoDecode] = Field(
        default=[], validation_alias="CALIBRE_WEB_CLI_MTP_USB_IDS"
    )
    snapshot_retention_days: int = Field(
        default=14, validation_alias="CALIBRE_WEB_CLI_SNAPSHOT_RETENTION_DAYS"
    )

    @field_validator("password", mode="before")
    @classmethod
    def _empty_password_is_none(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return value

    @field_validator("metadata_sources", "device_format_order", "mtp_usb_ids", mode="before")
    @classmethod
    def _parse_csv(cls, value: str | list[str] | None) -> list[str]:
        return _split_csv(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
