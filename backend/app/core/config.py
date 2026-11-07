"""
Application configuration and environment settings.
"""
import os
from pydantic import BaseModel, Field
from typing import Optional, List


class Settings(BaseModel):
    app_name: str = "Schwab & Alpaca Trader Hub"
    version: str = "1.1.0"
    debug: bool = True
    port: int = 8000
    host: str = "127.0.0.1"

    # Provider Toggles:
    # market_data_provider: "alpaca" | "simulation" | "schwab"
    # broker_provider: "alpaca_paper" | "virtual"
    market_data_provider: str = Field(default_factory=lambda: os.getenv("MARKET_DATA_PROVIDER", "alpaca"))
    broker_provider: str = Field(default_factory=lambda: os.getenv("BROKER_PROVIDER", "alpaca_paper"))

    # Trading configuration
    initial_capital: float = 100000.0  # Synced with Alpaca paper account ($100k)
    default_slippage_bps: float = 2.0   # 2 basis points for local virtual engine
    default_commission: float = 0.0     # $0 commission for US stocks

    # Alpaca API Configuration — credentials MUST come from the environment.
    # Never commit real keys to source control.
    alpaca_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("ALPACA_API_KEY"))
    alpaca_api_secret: Optional[str] = Field(default_factory=lambda: os.getenv("ALPACA_API_SECRET"))
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets/v2"
    alpaca_data_base_url: str = "https://data.alpaca.markets/v2"
    alpaca_data_stream_url: str = "wss://stream.data.alpaca.markets/v2/iex"
    alpaca_feed: str = "iex"

    # Schwab API Configuration (Optional: reserved for Schwab live OAuth)
    schwab_app_key: Optional[str] = Field(default_factory=lambda: os.getenv("SCHWAB_APP_KEY"))
    schwab_app_secret: Optional[str] = Field(default_factory=lambda: os.getenv("SCHWAB_APP_SECRET"))
    schwab_callback_url: str = "https://127.0.0.1:8182/"
    schwab_token_path: str = "token.json"

    # Access control for the trading API. The single-user terminal requires the
    # token entered in the browser (stored in localStorage) for every REST call
    # and for the WebSocket handshake. Empty = refuse all mutating requests.
    admin_token: Optional[str] = Field(default_factory=lambda: os.getenv("ADMIN_TOKEN"))

    # Browser origins allowed to call the API (comma separated, exact match).
    allowed_origins: List[str] = Field(
        default_factory=lambda: [
            o.strip()
            for o in os.getenv(
                "ALLOWED_ORIGINS",
                "https://trader.mangoqwq.com,http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if o.strip()
        ]
    )

    # Non-US markets (HK / JP / CN) are quoted through Yahoo Finance. Set to 0
    # to keep those venues dark without touching the US provider (used by the
    # test suite so it stays hermetic).
    intl_market_data_enabled: bool = Field(
        default_factory=lambda: os.getenv("INTL_MARKET_DATA", "1") not in ("0", "false", "False")
    )

    # Set to 0 to serve fallback FX rates without any network access.
    fx_enabled: bool = Field(
        default_factory=lambda: os.getenv("FX_ENABLED", "1") not in ("0", "false", "False")
    )

    # Where the virtual (HK / JP / CN) book is persisted between restarts.
    # Unset = resolve a writable directory at startup (see resolve_state_dir);
    # "off" disables persistence (used by the hermetic test suite).
    state_dir: str = Field(
        default_factory=lambda: os.getenv("STATE_DIR", "")
    )

    @property
    def state_persist_enabled(self) -> bool:
        return self.state_dir.strip().lower() not in ("off", "none", "0", "false")

    # Default tracked tickers
    default_symbols: List[str] = [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA",
        "AMD", "AMZN", "GOOGL", "META", "IWM", "COIN", "PLTR", "NFLX"
    ]


settings = Settings()
