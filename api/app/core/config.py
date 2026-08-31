from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    log_level: str = "INFO"
    admin_username: str = "admin"
    admin_password: str = "change_me"
    service_api_token: str = "change_me"
    payment_provider: str = "mock"
    payment_webhook_secret: str = "change_me"
    payment_auto_confirm: bool = False
    promo_codes: str = "WELCOME7:7"
    background_jobs_enabled: bool = False
    lifecycle_interval_seconds: int = 60
    lifecycle_advisory_lock_key: int = 846_202_608
    worker_run_once: bool = False
    threexui_api_token: str = ""
    threexui_verify_tls: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
