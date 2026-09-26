from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    log_level: str = "INFO"
    admin_username: str = "admin"
    admin_password: str = "change_me"
    service_api_token: str = "change_me"
    payment_provider: str = "mock"
    payment_webhook_secret: str = "change_me"
    payment_auto_confirm: bool = False
    telegram_stars_rate: float = Field(default=0.6, gt=0)
    promo_codes: str = "WELCOME7:7"
    background_jobs_enabled: bool = False
    lifecycle_interval_seconds: int = 60
    lifecycle_advisory_lock_key: int = 846_202_608
    subscription_expiration_reminder_days: int = Field(default=3, ge=1, le=30)
    worker_run_once: bool = False
    threexui_api_token: str = ""
    threexui_verify_tls: bool = True
    public_base_url: str = "http://localhost:8000"
    cabinet_token_ttl_days: int = 365
    cabinet_email_code_ttl_minutes: int = Field(default=10, ge=1, le=60)
    cabinet_allow_temporary_registration: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_use_ssl: bool = False
    bot_token: str = ""
    bot_username: str = "vpn142323srv_bot"
    bot_admin_chat_id: int = 0
    admin_notification_email: str = "freedmanofart5@gmail.com"
    provider_code_notifications_enabled: bool = False
    provider_code_notification_email: str = ""
    provider_name: str = "Freedom VPN"
    # Пустое значение означает deep link бота в сценарий техподдержки.
    provider_support_url: str = ""
    provider_cabinet_url: str = ""
    client_trial_enabled: bool = True
    client_trial_days: int = Field(default=30, ge=1, le=365)
    client_trial_traffic_limit_bytes: int = Field(default=0, ge=0)
    client_provider_name: str = ""
    client_announcement: str = ""
    client_announcement_url: str = ""
    client_channel_url: str = ""
    client_support_url: str = ""
    client_website_url: str = ""
    client_account_url: str = ""
    client_renewal_url: str = ""
    platega_enabled: bool = False
    platega_base_url: str = "https://app.platega.io"
    platega_merchant_id: str = ""
    platega_secret: str = ""
    platega_return_url: str = ""
    platega_failed_url: str = ""
    platega_callback_url: str = ""
    platega_method_sbp_qr: int = 2
    platega_method_mir_card: str = ""
    platega_method_crypto: int = 13

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
