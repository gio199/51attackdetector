from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .http import JsonHttpClient
from .models import Observation, utc_now


BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BITFINEX_SHORTS_URL = (
    "https://api-pub.bitfinex.com/v2/stats1/pos.size:1m:tBTCUSD:SHORT/last"
)
BITFINEX_LONGS_URL = (
    "https://api-pub.bitfinex.com/v2/stats1/pos.size:1m:tBTCUSD:LONG/last"
)
BYBIT_FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"
BYBIT_OI_URL = "https://api.bybit.com/v5/market/open-interest"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
OKX_OI_URL = "https://www.okx.com/api/v5/public/open-interest"


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} response must be an object")
    return value


class MarketContextCollector:
    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient()

    def _btc_price(self) -> float:
        response = _mapping(
            self.http.get(BINANCE_PRICE_URL, params={"symbol": "BTCUSDT"}),
            "BTC price",
        )
        return float(response["price"])

    def _bitfinex(self, btc_price: float) -> dict[str, float]:
        shorts = self.http.get(BITFINEX_SHORTS_URL)
        longs = self.http.get(BITFINEX_LONGS_URL)
        if not isinstance(shorts, list) or len(shorts) < 2:
            raise ValueError("Bitfinex shorts response is malformed")
        if not isinstance(longs, list) or len(longs) < 2:
            raise ValueError("Bitfinex longs response is malformed")
        return {
            "shorts_usd": abs(float(shorts[1])) * btc_price,
            "longs_usd": abs(float(longs[1])) * btc_price,
        }

    def _bybit(self, btc_price: float) -> dict[str, float]:
        funding = _mapping(
            self.http.get(
                BYBIT_FUNDING_URL,
                params={"category": "linear", "symbol": "BTCUSDT", "limit": 1},
            ),
            "Bybit funding",
        )
        open_interest = _mapping(
            self.http.get(
                BYBIT_OI_URL,
                params={
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "intervalTime": "5min",
                    "limit": 1,
                },
            ),
            "Bybit open interest",
        )
        funding_list = _mapping(funding.get("result"), "Bybit funding result").get(
            "list"
        )
        oi_list = _mapping(
            open_interest.get("result"), "Bybit open-interest result"
        ).get("list")
        if not isinstance(funding_list, list) or not funding_list:
            raise ValueError("Bybit funding list is empty")
        if not isinstance(oi_list, list) or not oi_list:
            raise ValueError("Bybit open-interest list is empty")
        return {
            "open_interest_usd": float(oi_list[0]["openInterest"]) * btc_price,
            "funding_rate": float(funding_list[0]["fundingRate"]),
        }

    def _okx(self) -> dict[str, float]:
        funding = _mapping(
            self.http.get(
                OKX_FUNDING_URL, params={"instId": "BTC-USDT-SWAP"}
            ),
            "OKX funding",
        )
        open_interest = _mapping(
            self.http.get(
                OKX_OI_URL,
                params={"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
            ),
            "OKX open interest",
        )
        funding_data = funding.get("data")
        oi_data = open_interest.get("data")
        if not isinstance(funding_data, list) or not funding_data:
            raise ValueError("OKX funding data is empty")
        if not isinstance(oi_data, list) or not oi_data:
            raise ValueError("OKX open-interest data is empty")
        oi_usd = oi_data[0].get("oiUsd")
        if oi_usd is None:
            raise ValueError("OKX open-interest response has no oiUsd")
        return {
            "open_interest_usd": float(oi_usd),
            "funding_rate": float(funding_data[0]["fundingRate"]),
        }

    def collect(self, *, now: datetime | None = None) -> Observation:
        observed_at = now or utc_now()
        errors: dict[str, str] = {}
        values: dict[str, Any] = {}
        try:
            btc_price = self._btc_price()
            values["btc_price_usd"] = btc_price
        except Exception as exc:
            return Observation.failed(
                "market_context",
                "binance/bitfinex/bybit/okx",
                f"BTC price unavailable: {exc}",
                observed_at=observed_at,
            )

        for name, call in (
            ("bitfinex_margin_positions", lambda: self._bitfinex(btc_price)),
            ("bybit_derivatives", lambda: self._bybit(btc_price)),
            ("okx_derivatives", self._okx),
        ):
            try:
                values[name] = call()
            except Exception as exc:
                errors[name] = str(exc)

        if len(values) == 1:
            return Observation.failed(
                "market_context",
                "binance/bitfinex/bybit/okx",
                "All derivatives sources failed",
                observed_at=observed_at,
                metadata={"errors": errors},
            )
        return Observation.ok(
            "market_context",
            "binance/bitfinex/bybit/okx",
            values,
            observed_at=observed_at,
            partial=bool(errors),
            metadata={
                "errors": errors,
                "description": (
                    "Open interest is total two-sided exposure; it is not split into invented long/short amounts."
                ),
            },
        )
