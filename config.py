import os
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    database_url: str = Field(default="sqlite+aiosqlite:///./tradeweek.db", alias="DATABASE_URL")
    weekly_grant_amount: str = Field(default="10000", alias="WEEKLY_GRANT_AMOUNT")
    prize_top_n: int = Field(default=10, alias="PRIZE_TOP_N")
    max_price_staleness_seconds: int = Field(default=3, alias="MAX_PRICE_STALENESS_SECONDS")
    price_poll_interval_seconds: int = Field(default=2, alias="PRICE_POLL_INTERVAL_SECONDS")
    min_24h_quote_volume_usdt: str = Field(default="1000000", alias="MIN_24H_QUOTE_VOLUME_USDT")
    week_reset_day: str = Field(default="monday", alias="WEEK_RESET_DAY")
    week_reset_time: str = Field(default="00:00", alias="WEEK_RESET_TIME")
    week_reset_tz: str = Field(default="UTC", alias="WEEK_RESET_TZ")
    admin_telegram_ids: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def database_url_async(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def admin_ids_set(self) -> set[int]:
        if not self.admin_telegram_ids.strip():
            return set()
        return {int(x.strip()) for x in self.admin_telegram_ids.split(",") if x.strip()}

    @property
    def weekly_grant_decimal(self):
        from decimal import Decimal
        return Decimal(self.weekly_grant_amount)

    @property
    def min_volume_decimal(self):
        from decimal import Decimal
        return Decimal(self.min_24h_quote_volume_usdt)

settings = Settings()
