import os
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    database_url: str = Field(default="sqlite+aiosqlite:///./tradeweek.db", alias="DATABASE_URL")
    trading_mode: str = Field(default="paper", alias="TRADING_MODE")
    initial_balance_usd: str = Field(default="10000", alias="INITIAL_BALANCE_USD")
    weekly_grant_amount: str = Field(default="10000", alias="WEEKLY_GRANT_AMOUNT")
    prize_top_n: int = Field(default=10, alias="PRIZE_TOP_N")
    paper_slippage_bps: int = Field(default=0, alias="PAPER_SLIPPAGE_BPS")
    market_data_provider: str = Field(default="bingx", alias="MARKET_DATA_PROVIDER")
    bingx_market_type: str = Field(default="perpetual", alias="BINGX_MARKET_TYPE")
    market_data_max_age_ms: int = Field(default=2000, alias="MARKET_DATA_MAX_AGE_MS")
    competition_ranking: str = Field(default="roi", alias="COMPETITION_RANKING")
    bingx_api_key: str = Field(default="", alias="BINGX_API_KEY")
    bingx_api_secret: str = Field(default="", alias="BINGX_API_SECRET")
    webapp_url: str = Field(default="", alias="WEBAPP_URL")
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
