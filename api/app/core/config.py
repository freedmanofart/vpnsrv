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
    xray_api_address: str = "172.18.0.1:10085"
    xray_inbound_tag: str = "vless-reality"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
