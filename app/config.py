"""Application settings and timezone helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or .env."""

    app_name: str = Field(
        default="Crush Lifestyle Fitness Studio API",
        validation_alias=AliasChoices("GYMBRO_APP_NAME", "APP_NAME"),
    )
    app_version: str = "2.0.0"
    timezone: str = Field(
        default="Asia/Kolkata",
        validation_alias=AliasChoices("GYMBRO_TIMEZONE", "TZ", "GENERIC_TIMEZONE"),
    )
    db_path: str = Field(
        default="./data/crush_fitness.db",
        validation_alias=AliasChoices("GYMBRO_DB_PATH", "DB_PATH"),
    )
    storage_backend: str = Field(
        default="google_sheets",
        validation_alias=AliasChoices("GYMBRO_STORAGE_BACKEND", "STORAGE_BACKEND"),
    )
    google_sheets_mode: str = Field(
        default="n8n_bridge",
        validation_alias=AliasChoices("GOOGLE_SHEETS_MODE", "GYMBRO_GOOGLE_SHEETS_MODE"),
    )
    google_sheets_spreadsheet_id: str = Field(
        default="REPLACE_WITH_GOOGLE_SHEET_ID",
        validation_alias=AliasChoices("GOOGLE_SHEETS_SPREADSHEET_ID", "GOOGLE_SHEET_ID"),
    )
    google_sheets_spreadsheet_name: str = Field(
        default="Crush Lifestyle Fitness Studio",
        validation_alias=AliasChoices("GOOGLE_SHEETS_SPREADSHEET_NAME"),
    )
    google_sheets_workbook_path: str = Field(
        default="./data/crush_fitness_sheets_workbook.json",
        validation_alias=AliasChoices("GOOGLE_SHEETS_WORKBOOK_PATH", "GYMBRO_WORKBOOK_PATH"),
    )
    google_sheets_bridge_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("GOOGLE_SHEETS_BRIDGE_WEBHOOK_URL"),
    )
    google_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GOOGLE_API_KEY"),
    )
    google_oauth_access_token: str = Field(
        default="",
        validation_alias=AliasChoices("GOOGLE_OAUTH_ACCESS_TOKEN"),
    )
    google_service_account_json_path: str = Field(
        default="",
        validation_alias=AliasChoices("GOOGLE_SERVICE_ACCOUNT_JSON_PATH"),
    )
    n8n_google_sheets_credential_id: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_GOOGLE_SHEETS_CREDENTIAL_ID"),
    )
    n8n_google_sheets_credential_name: str = Field(
        default="Google Sheets account",
        validation_alias=AliasChoices("N8N_GOOGLE_SHEETS_CREDENTIAL_NAME"),
    )
    n8n_whatsapp_credential_id: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_WHATSAPP_CREDENTIAL_ID"),
    )
    n8n_whatsapp_credential_name: str = Field(
        default="WhatsApp account",
        validation_alias=AliasChoices("N8N_WHATSAPP_CREDENTIAL_NAME"),
    )
    whatsapp_phone_number_id: str = Field(
        default="REPLACE_WITH_WHATSAPP_PHONE_NUMBER_ID",
        validation_alias=AliasChoices("WHATSAPP_PHONE_NUMBER_ID"),
    )
    notification_channel: str = Field(
        default="Email",
        validation_alias=AliasChoices("NOTIFICATION_CHANNEL", "GYMBRO_NOTIFICATION_CHANNEL"),
    )
    studio_sender_email: str = Field(
        default="crushfitnessstudio@gmail.com",
        validation_alias=AliasChoices("STUDIO_SENDER_EMAIL", "GYMBRO_SENDER_EMAIL"),
    )
    n8n_gmail_credential_id: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_GMAIL_CREDENTIAL_ID"),
    )
    n8n_gmail_credential_name: str = Field(
        default="Gmail account",
        validation_alias=AliasChoices("N8N_GMAIL_CREDENTIAL_NAME"),
    )
    gst_rate: float = Field(
        default=0.18,
        validation_alias=AliasChoices("GYMBRO_GST_RATE", "GST_RATE"),
    )
    api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GYMBRO_API_KEY", "API_KEY"),
    )

    n8n_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_BASE_URL", "GYMBRO_N8N_BASE_URL"),
    )
    n8n_gm01_sync_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_GM01_SYNC_WEBHOOK_URL", "GYMBRO_N8N_GM01_SYNC_WEBHOOK_URL"),
    )
    n8n_gm02_dispatch_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_GM02_DISPATCH_WEBHOOK_URL", "GYMBRO_N8N_GM02_DISPATCH_WEBHOOK_URL"),
    )
    n8n_gm03_payment_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_GM03_PAYMENT_WEBHOOK_URL", "GYMBRO_N8N_GM03_PAYMENT_WEBHOOK_URL"),
    )
    n8n_gm04_digest_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_GM04_DIGEST_WEBHOOK_URL", "GYMBRO_N8N_GM04_DIGEST_WEBHOOK_URL"),
    )
    n8n_gm05_agent_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_GM05_AGENT_WEBHOOK_URL", "GYMBRO_N8N_GM05_AGENT_WEBHOOK_URL"),
    )
    n8n_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_API_KEY", "GYMBRO_N8N_API_KEY"),
    )
    n8n_webhook_secret: str = Field(
        default="",
        validation_alias=AliasChoices("N8N_WEBHOOK_SECRET", "GYMBRO_N8N_WEBHOOK_SECRET"),
    )
    cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000,*",
        validation_alias=AliasChoices("CORS_ORIGINS", "GYMBRO_CORS_ORIGINS"),
    )
    razorpay_webhook_secret: str = Field(
        default="",
        validation_alias=AliasChoices("RAZORPAY_WEBHOOK_SECRET", "GYMBRO_RAZORPAY_WEBHOOK_SECRET"),
    )

    fastapi_public_url: str = Field(
        default="http://localhost:8000",
        validation_alias=AliasChoices("FASTAPI_PUBLIC_URL", "GYMBRO_PUBLIC_URL"),
    )
    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"),
    )
    llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_BASE_URL"),
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("LLM_MODEL", "OPENAI_MODEL"),
    )

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()


def now_ist() -> datetime:
    """Return the current timezone-aware datetime in Asia/Kolkata."""
    tz_name = get_settings().timezone
    try:
        return datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def today_ist_str() -> str:
    """Return today's calendar date in Asia/Kolkata as YYYY-MM-DD."""
    return now_ist().strftime("%Y-%m-%d")


def timestamp_ist_str() -> str:
    """Return an ISO-style IST timestamp string."""
    return now_ist().strftime("%Y-%m-%d %H:%M:%S IST")
