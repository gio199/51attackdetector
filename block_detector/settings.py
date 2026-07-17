from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    bitcoin_rpc_urls: tuple[str, ...] = ()
    bitcoin_rpc_user: str | None = None
    bitcoin_rpc_password: str | None = None
    bitcoin_network: str = "main"
    minimum_healthy_nodes: int = 2
    state_path: Path = Path(".block_detector_state.json")
    news_api_key: str | None = None
    blockchair_api_key: str | None = None
    expected_block_minutes: float = 10.0
    timing_watch_tail: float = 0.05
    timing_warning_tail: float = 0.01
    timing_critical_tail: float = 0.001
    block_history_limit: int = 24
    poll_seconds: float = 30.0
    repeat_alert_seconds: float = 900.0
    request_connect_timeout: float = 3.05
    request_read_timeout: float = 10.0
    max_reorg_search_depth: int = 144
    public_height_lag_blocks: int = 3
    core_absolute_stale_minutes: float = 180.0
    monitor_wallet_transactions: bool = False
    wallet_target_confirmations: int = 6

    @property
    def request_timeout(self) -> tuple[float, float]:
        return (self.request_connect_timeout, self.request_read_timeout)

    @classmethod
    def from_env(cls) -> "Settings":
        urls = tuple(
            url.strip()
            for url in os.getenv("BITCOIN_RPC_URLS", "").split(",")
            if url.strip()
        )
        return cls(
            bitcoin_rpc_urls=urls,
            bitcoin_rpc_user=os.getenv("BITCOIN_RPC_USER"),
            bitcoin_rpc_password=os.getenv("BITCOIN_RPC_PASSWORD"),
            bitcoin_network=os.getenv("BITCOIN_NETWORK", "main"),
            minimum_healthy_nodes=_int_env("MINIMUM_HEALTHY_NODES", 2),
            state_path=Path(
                os.getenv("BLOCK_DETECTOR_STATE_PATH", ".block_detector_state.json")
            ),
            news_api_key=os.getenv("NEWS_API_KEY"),
            blockchair_api_key=os.getenv("BLOCKCHAIR_API_KEY"),
            expected_block_minutes=_float_env("EXPECTED_BLOCK_MINUTES", 10.0),
            timing_watch_tail=_float_env("TIMING_WATCH_TAIL", 0.05),
            timing_warning_tail=_float_env("TIMING_WARNING_TAIL", 0.01),
            timing_critical_tail=_float_env("TIMING_CRITICAL_TAIL", 0.001),
            block_history_limit=_int_env("BLOCK_HISTORY_LIMIT", 24),
            poll_seconds=_float_env("POLL_SECONDS", 30.0),
            repeat_alert_seconds=_float_env("REPEAT_ALERT_SECONDS", 900.0),
            request_connect_timeout=_float_env(
                "REQUEST_CONNECT_TIMEOUT", 3.05
            ),
            request_read_timeout=_float_env("REQUEST_READ_TIMEOUT", 10.0),
            max_reorg_search_depth=_int_env("MAX_REORG_SEARCH_DEPTH", 144),
            public_height_lag_blocks=_int_env(
                "PUBLIC_HEIGHT_LAG_BLOCKS", 3
            ),
            core_absolute_stale_minutes=_float_env(
                "CORE_ABSOLUTE_STALE_MINUTES", 180.0
            ),
            monitor_wallet_transactions=_bool_env(
                "MONITOR_WALLET_TRANSACTIONS", False
            ),
            wallet_target_confirmations=_int_env(
                "WALLET_TARGET_CONFIRMATIONS", 6
            ),
        )
