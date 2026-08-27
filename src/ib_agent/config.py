"""Runtime configuration for ib_agent, loaded from environment / .env file.

No IBKR credentials live here: the username/password are only known to IBC
(~/ibc/config.ini), which is what performs the unattended Gateway login.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")

# IB Gateway API ports. TWS uses 7496/7497.
PAPER_PORTS = {4002, 7497}


@dataclass(frozen=True)
class Settings:
    """Everything the agent needs to reach the Gateway and store snapshots."""

    host: str
    port: int
    client_id: int
    account: str
    readonly: bool
    connect_timeout: float
    auto_start_gateway: bool
    market_data_type: int
    data_dir: Path
    db_path: Path
    flex_token: str | None
    flex_query_id: str | None
    # The headless X display IBC's Gateway draws on. Only the login path needs
    # it: that is where the 2FA dialog appears (see login.py).
    xvfb_display: str = ":99"

    @property
    def trading_mode(self) -> str:
        return "paper" if self.port in PAPER_PORTS else "live"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    data_dir = Path(os.getenv("IB_DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser()
    return Settings(
        host=os.getenv("IB_HOST", "127.0.0.1"),
        port=int(os.getenv("IB_PORT", "4001")),
        # A dedicated client id keeps this reader from clashing with other
        # API clients on the same Gateway.
        client_id=int(os.getenv("IB_CLIENT_ID", "17")),
        account=os.getenv("IB_ACCOUNT", ""),
        readonly=_env_bool("IB_READONLY", True),
        connect_timeout=float(os.getenv("IB_CONNECT_TIMEOUT", "20")),
        auto_start_gateway=_env_bool("IB_AUTO_START_GATEWAY", True),
        # 1 live, 2 frozen, 3 delayed (falls back to live when subscribed),
        # 4 delayed-frozen. 3 is the safe default: quotes never fail because a
        # market data subscription is missing.
        market_data_type=int(os.getenv("IB_MARKET_DATA_TYPE", "3")),
        data_dir=data_dir,
        db_path=Path(os.getenv("IB_DB_PATH", str(data_dir / "portfolio.sqlite3"))),
        flex_token=os.getenv("IB_FLEX_TOKEN") or None,
        flex_query_id=os.getenv("IB_FLEX_QUERY_ID") or None,
        xvfb_display=os.getenv("XVFB_DISPLAY", ":99"),
    )
