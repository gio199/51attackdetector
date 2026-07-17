from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from block_detector.collectors import (
    BlackoutNewsCollector,
    PoolDistributionCollector,
    PublicBlockHashCollector,
    RecentBlocksCollector,
    calculate_hashrate_metrics,
    collect_research_context,
)


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class CollectorTests(unittest.TestCase):
    def test_hashrate_uses_time_windows_not_rows(self) -> None:
        as_of = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)

        def point(days: int, value: float) -> dict[str, float]:
            timestamp = int((as_of - timedelta(days=days)).timestamp())
            return {"x": timestamp, "y": value}

        metrics = calculate_hashrate_metrics(
            [
                point(20, 50),
                point(1, 100),
                point(31, 40),
                point(8, 60),
                point(3, 80),
            ],
            as_of=as_of,
        )
        self.assertEqual(metrics["current"], 100)
        self.assertEqual(metrics["weekly_average"], 90)
        self.assertEqual(metrics["weekly_sample_count"], 2)
        self.assertEqual(metrics["monthly_sample_count"], 4)
        self.assertEqual(metrics["monthly_average"], 72.5)

    def test_recent_blocks_produce_n_minus_one_intervals(self) -> None:
        now = datetime(2026, 2, 1, 12, tzinfo=timezone.utc)
        response = {
            "data": [
                {"id": 102, "time": "2026-02-01 11:59:00"},
                {"id": 101, "time": "2026-02-01 11:49:00"},
                {"id": 100, "time": "2026-02-01 11:40:00"},
            ]
        }
        observation = RecentBlocksCollector(FakeHttp(response), limit=3).collect(
            now=now
        )
        self.assertTrue(observation.available)
        self.assertEqual(observation.value["block_count"], 3)
        self.assertEqual(observation.value["interval_count"], 2)
        self.assertEqual(observation.value["intervals_minutes"], [10.0, 9.0])

    def test_public_block_hash_selects_requested_height(self) -> None:
        observation = PublicBlockHashCollector(
            FakeHttp(
                [
                    {"height": 102, "id": "hash-102"},
                    {"height": 101, "id": "hash-101"},
                ]
            )
        ).collect(
            height=101,
            now=datetime(2026, 2, 1, 12, tzinfo=timezone.utc),
        )
        self.assertTrue(observation.available)
        self.assertEqual(observation.value["height"], 101)
        self.assertEqual(observation.value["hash"], "hash-101")

    def test_keyless_blackout_collector_uses_gdelt(self) -> None:
        response = {
            "articles": [
                {
                    "url": "https://example.test/a",
                    "title": "Power outage reported in Texas",
                    "seendate": "20260101T120000Z",
                    "domain": "example.test",
                    "sourcecountry": "US",
                }
            ]
        }
        http = FakeHttp(response)
        observation = BlackoutNewsCollector(None, http).collect(
            now=datetime(2026, 1, 1, 13, tzinfo=timezone.utc)
        )
        self.assertTrue(observation.available)
        self.assertEqual(observation.source, "gdeltproject.org/doc-api")
        self.assertEqual(observation.value["article_count"], 1)
        self.assertNotIn("apiKey", http.calls[0][1].get("params", {}))

    def test_keyless_news_requires_keyword_and_configured_region(self) -> None:
        response = {
            "articles": [
                {
                    "url": "https://example.test/unmatched",
                    "title": "Power outage reported in an unspecified area",
                    "seendate": "20260101T120000Z",
                    "domain": "example.test",
                }
            ]
        }
        observation = BlackoutNewsCollector(None, FakeHttp(response)).collect(
            now=datetime(2026, 1, 1, 13, tzinfo=timezone.utc)
        )
        self.assertTrue(observation.available)
        self.assertEqual(observation.value["article_count"], 0)

    def test_unattributed_blocks_are_not_scored_as_one_pool(self) -> None:
        observation = PoolDistributionCollector(
            FakeHttp({"Unknown": 80, "Pool A": 20})
        ).collect(now=datetime(2026, 1, 1, 13, tzinfo=timezone.utc))
        self.assertTrue(observation.available)
        self.assertEqual(observation.value["largest_pool"], "Pool A")
        self.assertEqual(observation.value["largest_share_percent"], 20)
        self.assertEqual(observation.value["unattributed_share_percent"], 80)
        self.assertAlmostEqual(observation.value["herfindahl_index"], 0.04)

    def test_recent_blocks_fall_back_to_mempool_space(self) -> None:
        now = datetime(2026, 2, 1, 12, tzinfo=timezone.utc)

        class FallbackHttp:
            def get(self, url, **kwargs):
                if "blockchair" in url:
                    raise RuntimeError("rate limited")
                return [
                    {
                        "height": 102,
                        "timestamp": int(
                            datetime(
                                2026, 2, 1, 11, 59, tzinfo=timezone.utc
                            ).timestamp()
                        ),
                    },
                    {
                        "height": 101,
                        "timestamp": int(
                            datetime(
                                2026, 2, 1, 11, 49, tzinfo=timezone.utc
                            ).timestamp()
                        ),
                    },
                ]

        observation = RecentBlocksCollector(FallbackHttp(), limit=2).collect(
            now=now
        )
        self.assertTrue(observation.available)
        self.assertEqual(observation.source, "mempool.space/api/blocks")
        self.assertEqual(observation.value["intervals_minutes"], [10.0])

    def test_halving_and_utc_context_are_explicit(self) -> None:
        observation = collect_research_context(
            840_000,
            now=datetime(2026, 2, 1, 3, tzinfo=timezone.utc),
        )
        self.assertTrue(observation.value["near_halving"])
        self.assertEqual(observation.value["blocks_since_halving"], 0)
        self.assertTrue(observation.value["in_research_time_window"])


if __name__ == "__main__":
    unittest.main()
