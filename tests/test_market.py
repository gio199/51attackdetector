from __future__ import annotations

import unittest

from block_detector.market import (
    BINANCE_PRICE_URL,
    BITFINEX_LONGS_URL,
    BITFINEX_SHORTS_URL,
    BYBIT_FUNDING_URL,
    BYBIT_OI_URL,
    OKX_FUNDING_URL,
    OKX_OI_URL,
    MarketContextCollector,
)


class MarketHttp:
    def get(self, url, **kwargs):
        responses = {
            BINANCE_PRICE_URL: {"price": "100000"},
            BITFINEX_SHORTS_URL: [0, -2],
            BITFINEX_LONGS_URL: [0, 3],
            BYBIT_FUNDING_URL: {
                "result": {"list": [{"fundingRate": "0.0001"}]}
            },
            BYBIT_OI_URL: {
                "result": {"list": [{"openInterest": "4"}]}
            },
            OKX_FUNDING_URL: {"data": [{"fundingRate": "-0.0002"}]},
            OKX_OI_URL: {"data": [{"oiUsd": "500000"}]},
        }
        return responses[url]


class MarketContextTests(unittest.TestCase):
    def test_open_interest_is_normalized_but_not_split(self) -> None:
        observation = MarketContextCollector(MarketHttp()).collect()
        self.assertTrue(observation.available)
        self.assertEqual(
            observation.value["bybit_derivatives"]["open_interest_usd"],
            400000,
        )
        self.assertEqual(
            observation.value["okx_derivatives"]["open_interest_usd"],
            500000,
        )
        self.assertNotIn("shorts", observation.value["bybit_derivatives"])
        self.assertNotIn("longs", observation.value["okx_derivatives"])


if __name__ == "__main__":
    unittest.main()
