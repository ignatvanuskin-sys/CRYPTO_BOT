import os
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    database_url: str = Field(default="sqlite+aiosqlite:///./paper.db", alias="DATABASE_URL")
    require_postgres: bool = Field(default=False, alias="REQUIRE_POSTGRES")
    initial_balance_usd: str = Field(default="10000", alias="INITIAL_BALANCE_USD")
    paper_slippage_bps: int = Field(default=0, alias="PAPER_SLIPPAGE_BPS")
    bingx_market_type: str = Field(default="perpetual", alias="BINGX_MARKET_TYPE")
    market_data_max_age_ms: int = Field(default=10000, alias="MARKET_DATA_MAX_AGE_MS")
    price_poll_interval_seconds: int = Field(default=2, alias="PRICE_POLL_INTERVAL_SECONDS")
    admin_telegram_ids: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")
    demo_seed_enabled: bool = Field(default=False, alias="DEMO_SEED_ENABLED")
    demo_player_count: int = Field(default=20, alias="DEMO_PLAYER_COUNT")
    demo_cup_duration_hours: int = Field(default=24, alias="DEMO_CUP_DURATION_HOURS")
    demo_prize_pool: str = Field(default="100", alias="DEMO_PRIZE_POOL")

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
        ids: set[int] = set()
        for raw_id in self.admin_telegram_ids.split(","):
            raw_id = raw_id.strip()
            if not raw_id:
                continue
            try:
                ids.add(int(raw_id))
            except ValueError:
                # Malformed admin configuration must not grant access.
                continue
        return ids

    @property
    def database_is_postgres(self) -> bool:
        return self.database_url_async.startswith("postgresql+")

settings = Settings()
