from __future__ import annotations

import unittest
from unittest.mock import patch

from block_detector.ai import AIRecommendation
from block_detector.cli import main
from block_detector.report import render_report


def report_snapshot() -> dict[str, object]:
    return {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "assessment": {
            "level": "watch",
            "evidence_score": 18,
            "data_quality": "degraded_context",
            "summary": "Anomalies are present.",
            "reasons": ["Block timing is unusual."],
            "actions": ["Increase monitoring frequency."],
            "confirmation_multiplier": 1.5,
            "pause_settlement": False,
            "score_components": [
                {
                    "signal": "block_timing",
                    "category": "context",
                    "points": 18,
                    "rule": "add:18",
                    "detail": "Block timing is unusual.",
                }
            ],
        },
        "observations": {
            "chain_signals": {
                "status": "ok",
                "value": {
                    "healthy_node_count": 2,
                    "configured_node_count": 3,
                    "minimum_healthy_nodes": 2,
                    "quorum_met": True,
                    "common_height": 900000,
                    "hashes_at_common_height": {"node-1": "a", "node-2": "a"},
                    "node_divergence": False,
                    "height_spread": 0,
                    "max_reorg_depth": 0,
                    "max_valid_fork_branch_length": 0,
                    "wallet_monitoring_enabled": False,
                    "nodes": [],
                },
            },
            "chain_public_consistency": {
                "status": "ok",
                "value": {
                    "public_height": 900000,
                    "highest_core_height": 900000,
                    "lowest_core_height": 900000,
                    "public_reference_available": True,
                    "maximum_public_minus_core_blocks": 0,
                    "minimum_public_minus_core_blocks": 0,
                    "lag_threshold_blocks": 3,
                    "required_core_quorum": 2,
                    "public_height_fresh_node_count": 2,
                    "public_height_fresh_quorum_met": True,
                    "core_quorum_materially_behind_public": False,
                    "public_reference_materially_behind_core": False,
                    "lineage_height": 900000,
                    "lineage_source_status": "ok",
                    "lineage_source_kind": "mempool.space/api/blocks",
                    "public_lineage_sources_disagree": False,
                    "lineage_hash_comparison_count": 2,
                    "lineage_hash_match_count": 2,
                    "lineage_hash_mismatch_count": 0,
                    "public_core_lineage_mismatch": False,
                    "public_core_lineage_comparison_incomplete": False,
                },
            },
            "public_chain_tip": {
                "status": "ok",
                "value": {
                    "height": 900000,
                    "age_minutes": 70,
                    "block_time": "2026-01-01T00:00:00+00:00",
                    "future_header_timestamp": False,
                },
            },
            "block_timing": {
                "status": "ok",
                "value": {
                    "level": "critical",
                    "age_tail_probability": 0.0009,
                    "recent_average_tail_probability": 0.2,
                    "reason": "Long current wait.",
                },
            },
            "recent_block_intervals": {
                "status": "partial",
                "value": {
                    "intervals_minutes": [10.0] * 10,
                    "average_minutes": 10.0,
                    "median_minutes": 10.0,
                    "p95_minutes": 10.0,
                },
                "metadata": {"primary_error": "primary rate limited"},
            },
            "network_hashrate": {
                "status": "ok",
                "unit": "TH/s",
                "value": {
                    "current": 90,
                    "weekly_average": 95,
                    "monthly_average": 100,
                    "change_from_weekly_percent": -5.26,
                    "change_from_monthly_percent": -10,
                    "age_hours": 1,
                },
            },
            "mining_pool_distribution": {
                "status": "ok",
                "value": {
                    "largest_pool": "Pool A",
                    "largest_share_percent": 31,
                    "observed_blocks": 500,
                    "herfindahl_index": 0.14,
                },
            },
            "nicehash_sha256_context": {
                "status": "unavailable",
                "error": "fixture unavailable",
            },
            "market_context": {
                "status": "ok",
                "value": {
                    "btc_price_usd": 100000,
                    "bitfinex_margin_positions": {
                        "shorts_usd": 200000,
                        "longs_usd": 400000,
                    },
                },
            },
            "bitcoin_cash_context": {
                "status": "ok",
                "value": {
                    "market_price_usd": 500,
                    "hashrate_24h": 4,
                    "average_transaction_fee_24h": 0.01,
                },
            },
            "research_context": {
                "status": "ok",
                "value": {
                    "utc_hour": 3,
                    "in_research_time_window": True,
                    "near_halving": False,
                    "blocks_until_halving": 100000,
                    "blocks_since_halving": 110000,
                },
            },
            "blackout_news": {
                "status": "ok",
                "source": "gdelt",
                "value": {
                    "article_count": 1,
                    "days_back": 2,
                    "articles": [
                        {
                            "title": "Power outage in Texas",
                            "locations": ["Texas"],
                        }
                    ],
                },
            },
        },
    }


class ReportTests(unittest.TestCase):
    def test_report_displays_score_signals_actions_and_ai(self) -> None:
        report = render_report(
            report_snapshot(),
            AIRecommendation(
                status="ok",
                model="test-model",
                summary="Verify the anomaly.",
                checks=("Compare node tips.",),
                caveats=("Context is not proof.",),
            ),
        )
        self.assertIn("EVIDENCE SCORE: 18/100", report)
        self.assertIn("DIRECT CHAIN EVIDENCE", report)
        self.assertIn("Core/public height cross-check", report)
        self.assertIn(
            "Common-height Core/public lineage checks", report
        )
        self.assertIn("Original-paper average/current hashrate ratio", report)
        self.assertIn("Power outage in Texas", report)
        self.assertIn("DETERMINISTIC RECOMMENDATIONS", report)
        self.assertIn("OpenAI model: test-model", report)
        self.assertIn("primary source failed; fallback used", report)

    def test_report_survives_missing_ai(self) -> None:
        report = render_report(
            report_snapshot(),
            AIRecommendation.unavailable("OPENAI_API_KEY is not configured"),
        )
        self.assertIn("UNAVAILABLE: OPENAI_API_KEY", report)
        self.assertIn("deterministic recommendations", report.lower())

    def test_report_strips_terminal_control_sequences(self) -> None:
        snapshot = report_snapshot()
        observations = snapshot["observations"]
        news = observations["blackout_news"]["value"]
        news["articles"][0]["title"] = "\x1b[31mALERT\x1b[0m\u202e"
        report = render_report(
            snapshot,
            AIRecommendation(
                status="ok",
                summary="\x1b[2JReview safely.",
            ),
        )
        self.assertIn("ALERT", report)
        self.assertIn("Review safely.", report)
        self.assertNotIn("\x1b", report)
        self.assertNotIn("[31m", report)
        self.assertNotIn("\u202e", report)

    def test_no_arguments_runs_the_unified_report(self) -> None:
        with patch("block_detector.cli.report_main", return_value=0) as report:
            self.assertEqual(main([]), 0)
        report.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
