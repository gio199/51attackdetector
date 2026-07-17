from __future__ import annotations

import unittest

from block_detector.models import Observation, RiskLevel
from block_detector.policy import assess_risk


def observation(name: str, value: dict) -> Observation:
    return Observation.ok(name, "test", value)


class PolicyTests(unittest.TestCase):
    def test_missing_direct_evidence_is_unknown(self) -> None:
        assessment = assess_risk(
            {
                "block_timing": observation(
                    "block_timing", {"level": RiskLevel.NORMAL.value}
                )
            }
        )
        self.assertEqual(assessment.level, RiskLevel.UNKNOWN)
        self.assertEqual(
            assessment.data_quality, "direct_chain_signals_unavailable"
        )

    def test_context_score_cannot_override_missing_direct_evidence(self) -> None:
        assessment = assess_risk(
            {
                "block_timing": observation(
                    "block_timing", {"level": RiskLevel.CRITICAL.value}
                ),
                "network_hashrate": observation(
                    "network_hashrate",
                    {"change_from_monthly_percent": -35},
                ),
                "mining_pool_distribution": observation(
                    "mining_pool_distribution",
                    {
                        "largest_pool": "Pool A",
                        "largest_share_percent": 45,
                    },
                ),
                "blackout_news": observation(
                    "blackout_news", {"article_count": 5}
                ),
                "research_context": observation(
                    "research_context",
                    {
                        "near_halving": True,
                        "in_research_time_window": True,
                    },
                ),
            }
        )
        self.assertEqual(assessment.evidence_score, 52)
        self.assertEqual(assessment.level, RiskLevel.UNKNOWN)

    def test_partial_direct_observation_is_not_sufficient_quality(self) -> None:
        assessment = assess_risk(
            {
                "chain_signals": Observation.ok(
                    "chain_signals",
                    "test",
                    {
                        "quorum_met": True,
                        "max_reorg_depth": 0,
                        "max_valid_fork_branch_length": 0,
                        "node_divergence": False,
                    },
                    partial=True,
                )
            }
        )
        self.assertEqual(assessment.level, RiskLevel.NORMAL)
        self.assertEqual(
            assessment.data_quality, "degraded_direct_chain_data"
        )

    def test_quiet_single_node_is_unknown(self) -> None:
        assessment = assess_risk(
            {
                "chain_signals": observation(
                    "chain_signals",
                    {
                        "quorum_met": True,
                        "common_height_comparison_count": 1,
                        "max_reorg_depth": 0,
                        "max_valid_fork_branch_length": 0,
                        "node_divergence": False,
                    },
                )
            }
        )
        self.assertEqual(assessment.level, RiskLevel.UNKNOWN)
        self.assertEqual(assessment.data_quality, "single_node_only")

    def test_quiet_quorum_is_normal(self) -> None:
        assessment = assess_risk(
            {
                "chain_signals": observation(
                    "chain_signals",
                    {
                        "quorum_met": True,
                        "max_reorg_depth": 0,
                        "max_valid_fork_branch_length": 0,
                        "node_divergence": False,
                    },
                ),
                "block_timing": observation(
                    "block_timing", {"level": RiskLevel.NORMAL.value}
                ),
            }
        )
        self.assertEqual(assessment.level, RiskLevel.NORMAL)

    def test_unattributed_pool_category_does_not_add_points(self) -> None:
        assessment = assess_risk(
            {
                "chain_signals": observation(
                    "chain_signals",
                    {
                        "quorum_met": True,
                        "max_reorg_depth": 0,
                        "max_valid_fork_branch_length": 0,
                        "node_divergence": False,
                    },
                ),
                "mining_pool_distribution": observation(
                    "mining_pool_distribution",
                    {
                        "largest_pool": "Unknown",
                        "largest_share_percent": 80,
                    },
                ),
            }
        )
        self.assertEqual(assessment.level, RiskLevel.NORMAL)
        self.assertEqual(assessment.evidence_score, 0)

    def test_extreme_timing_alone_is_not_critical(self) -> None:
        assessment = assess_risk(
            {
                "chain_signals": observation(
                    "chain_signals",
                    {
                        "quorum_met": True,
                        "max_reorg_depth": 0,
                        "max_valid_fork_branch_length": 0,
                        "node_divergence": False,
                    },
                ),
                "block_timing": observation(
                    "block_timing", {"level": RiskLevel.CRITICAL.value}
                ),
            }
        )
        self.assertEqual(assessment.level, RiskLevel.WATCH)
        self.assertEqual(assessment.evidence_score, 18)
        self.assertEqual(assessment.score_components[0].signal, "block_timing")
        self.assertEqual(assessment.score_components[0].points, 18)

    def test_two_block_reorg_warns(self) -> None:
        assessment = assess_risk(
            {
                "chain_signals": observation(
                    "chain_signals",
                    {
                        "quorum_met": True,
                        "max_reorg_depth": 2,
                        "max_valid_fork_branch_length": 0,
                        "node_divergence": False,
                    },
                )
            }
        )
        self.assertEqual(assessment.level, RiskLevel.WARNING)
        self.assertEqual(assessment.confirmation_multiplier, 2.0)

    def test_deep_reorg_pauses_settlement(self) -> None:
        assessment = assess_risk(
            {
                "chain_signals": observation(
                    "chain_signals",
                    {
                        "quorum_met": True,
                        "max_reorg_depth": 6,
                        "max_valid_fork_branch_length": 0,
                        "node_divergence": False,
                    },
                )
            }
        )
        self.assertEqual(assessment.level, RiskLevel.CRITICAL)
        self.assertTrue(assessment.pause_settlement)
        self.assertEqual(assessment.evidence_score, 100)
        self.assertEqual(sum(item.points for item in assessment.score_components), 100)


if __name__ == "__main__":
    unittest.main()
