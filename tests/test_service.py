from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from block_detector.models import Observation
from block_detector.policy import assess_risk
from block_detector.service import AlertGate, MonitorService
from block_detector.service import compare_core_to_public_tip
from block_detector.settings import Settings


class StaticCollector:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def collect(self, **kwargs):
        if self.error:
            raise self.error
        return self.result


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class CountingBlockHashCollector:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, *, height: int, now: datetime) -> Observation:
        self.calls += 1
        return Observation.ok(
            "public_common_height_hash",
            "test",
            {"height": height, "hash": f"hash-{self.calls}"},
            observed_at=now,
        )


class ServiceTests(unittest.TestCase):
    def _service(self, recent_error: Exception | None = None) -> MonitorService:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        public = Observation.ok(
            "public_chain_tip",
            "test",
            {
                "height": 100,
                "hash": "tip",
                "block_time": now.isoformat(),
                "age_minutes": 10.0,
            },
            observed_at=now,
        )
        recent = Observation.ok(
            "recent_block_intervals",
            "test",
            {
                "intervals_minutes": [8.0, 10.0, 12.0],
                "average_minutes": 10.0,
            },
            observed_at=now,
        )
        empty = Observation.ok("context", "test", {}, observed_at=now)
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
            },
            observed_at=now,
        )
        return MonitorService(
            Settings(minimum_healthy_nodes=1),
            public_chain=StaticCollector(public),
            recent_blocks=StaticCollector(recent, recent_error),
            hashrate=StaticCollector(empty),
            pools=StaticCollector(empty),
            blackouts=StaticCollector(empty),
            bitcoin_cash=StaticCollector(empty),
            nicehash=StaticCollector(empty),
            market=StaticCollector(empty),
            chain_monitor=StaticCollector(chain),
            monotonic=lambda: 0.0,
        )

    def test_collector_failure_is_isolated(self) -> None:
        snapshot = self._service(RuntimeError("fixture failure")).collect(
            now=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        recent = snapshot["observations"]["recent_block_intervals"]
        self.assertEqual(recent["status"], "error")
        self.assertEqual(snapshot["assessment"]["level"], "normal")
        self.assertEqual(
            snapshot["observations"]["block_timing"]["status"], "partial"
        )

    def test_alert_gate_debounces_and_repeats(self) -> None:
        clock = Clock()
        gate = AlertGate(60, monotonic=clock)
        snapshot = {
            "assessment": {
                "level": "warning",
                "reasons": ["test"],
            },
            "observations": {
                "public_chain_tip": {"value": {"hash": "tip"}}
            },
        }
        self.assertTrue(gate.should_emit(snapshot))
        self.assertFalse(gate.should_emit(snapshot))
        clock.value = 61
        self.assertTrue(gate.should_emit(snapshot))

    def test_stalled_core_nodes_are_compared_with_public_height(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        old_block_time = int((now - timedelta(hours=6)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height_comparison_count": 2,
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {"height": 100, "block_time": old_block_time},
                    {"height": 100, "block_time": old_block_time},
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {"height": 104},
            observed_at=now,
        )
        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            observed_at=now,
            lag_threshold_blocks=3,
        )
        self.assertEqual(consistency.status.value, "partial")
        self.assertEqual(
            consistency.value["maximum_public_minus_core_blocks"], 4
        )
        self.assertEqual(
            consistency.value["public_height_fresh_node_count"], 0
        )
        self.assertTrue(
            consistency.value[
                "core_quorum_materially_behind_public"
            ]
        )
        self.assertEqual(
            consistency.value["oldest_core_tip_age_minutes"], 360
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )
        self.assertEqual(assessment.level.value, "watch")
        self.assertEqual(assessment.evidence_score, 15)
        self.assertEqual(
            assessment.data_quality, "core_nodes_lag_public_reference"
        )

    def test_one_current_core_node_does_not_mask_a_stale_peer(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height_comparison_count": 2,
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 100,
                        "best_hash": "old-core-tip",
                        "block_time": int(
                            (now - timedelta(hours=6)).timestamp()
                        ),
                    },
                    {
                        "height": 104,
                        "best_hash": "public-tip",
                        "block_time": int(
                            (now - timedelta(minutes=5)).timestamp()
                        ),
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {"height": 104, "hash": "public-tip"},
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            observed_at=now,
            lag_threshold_blocks=3,
        )

        self.assertEqual(
            consistency.value["public_height_fresh_node_count"], 1
        )
        self.assertFalse(
            consistency.value["public_height_fresh_quorum_met"]
        )
        self.assertTrue(
            consistency.value[
                "core_quorum_materially_behind_public"
            ]
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )
        self.assertEqual(assessment.level.value, "watch")
        self.assertEqual(assessment.evidence_score, 15)

    def test_same_height_public_hash_mismatch_enters_watch(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        recent_time = int((now - timedelta(minutes=5)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height_comparison_count": 2,
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 104,
                        "best_hash": "core-a",
                        "block_time": recent_time,
                    },
                    {
                        "height": 104,
                        "best_hash": "core-a",
                        "block_time": recent_time,
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {"height": 104, "hash": "public-b"},
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            observed_at=now,
        )

        self.assertEqual(
            consistency.value["lineage_hash_mismatch_count"], 2
        )
        self.assertTrue(
            consistency.value["public_core_lineage_mismatch"]
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )
        self.assertEqual(assessment.level.value, "watch")
        self.assertEqual(assessment.evidence_score, 15)
        self.assertEqual(
            assessment.data_quality, "core_public_lineage_mismatch"
        )

    def test_old_core_quorum_is_detected_without_public_reference(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        old_time = int((now - timedelta(hours=6)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height_comparison_count": 2,
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 100,
                        "best_hash": "tip",
                        "block_time": old_time,
                    },
                    {
                        "height": 100,
                        "best_hash": "tip",
                        "block_time": old_time,
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.unavailable(
            "public_chain_tip",
            "test",
            "fixture unavailable",
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            observed_at=now,
            absolute_stale_minutes=180,
        )

        self.assertEqual(consistency.status.value, "partial")
        self.assertFalse(
            consistency.value["public_reference_available"]
        )
        self.assertTrue(
            consistency.value["core_quorum_extremely_stale"]
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )
        self.assertEqual(assessment.level.value, "watch")
        self.assertEqual(assessment.evidence_score, 15)
        self.assertEqual(
            assessment.data_quality,
            "core_tip_age_not_publicly_reconciled",
        )

    def test_single_core_node_ahead_does_not_claim_independent_agreement(
        self,
    ) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 1,
                "common_height_comparison_count": 1,
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 104,
                        "best_hash": "core-tip",
                        "block_time": int(now.timestamp()),
                    }
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {"height": 100, "hash": "public-tip"},
            observed_at=now,
        )
        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            observed_at=now,
        )

        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )

        self.assertEqual(assessment.level.value, "unknown")
        self.assertEqual(assessment.data_quality, "single_node_only")
        self.assertFalse(
            any(
                "independently agreed" in action
                for action in assessment.actions
            )
        )

    def test_public_lineage_is_checked_at_core_common_height(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        recent_time = int((now - timedelta(minutes=5)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height": 100,
                "common_height_comparison_count": 2,
                "hashes_at_common_height": {
                    "node-a": "core-lineage",
                    "node-b": "core-lineage",
                },
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 100,
                        "best_hash": "core-lineage",
                        "block_time": recent_time,
                    },
                    {
                        "height": 100,
                        "best_hash": "core-lineage",
                        "block_time": recent_time,
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {
                "height": 102,
                "hash": "public-tip",
                "age_minutes": 5,
            },
            observed_at=now,
        )
        public_common_hash = Observation.ok(
            "public_common_height_hash",
            "test",
            {"height": 100, "hash": "different-lineage"},
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            public_common_hash,
            observed_at=now,
            lag_threshold_blocks=3,
        )

        self.assertTrue(
            consistency.value[
                "public_core_lineage_comparison_complete"
            ]
        )
        self.assertEqual(
            consistency.value["lineage_hash_mismatch_count"], 2
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )
        self.assertEqual(assessment.level.value, "watch")
        self.assertEqual(assessment.evidence_score, 15)

    def test_disagreeing_public_hash_sources_cannot_remain_normal(
        self,
    ) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        recent_time = int((now - timedelta(minutes=5)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height": 100,
                "common_height_comparison_count": 2,
                "hashes_at_common_height": {
                    "node-a": "fresh-public-tip",
                    "node-b": "fresh-public-tip",
                },
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 100,
                        "best_hash": "fresh-public-tip",
                        "block_time": recent_time,
                    },
                    {
                        "height": 100,
                        "best_hash": "fresh-public-tip",
                        "block_time": recent_time,
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {
                "height": 100,
                "hash": "fresh-public-tip",
                "age_minutes": 5,
            },
            observed_at=now,
        )
        stale_common_hash = Observation.ok(
            "public_common_height_hash",
            "test",
            {"height": 100, "hash": "stale-public-hash"},
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            stale_common_hash,
            observed_at=now,
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )

        self.assertTrue(
            consistency.value["public_lineage_sources_disagree"]
        )
        self.assertEqual(
            consistency.value["lineage_hash_mismatch_count"], 0
        )
        self.assertEqual(assessment.level.value, "watch")
        self.assertEqual(assessment.evidence_score, 15)
        self.assertEqual(
            assessment.data_quality,
            "public_lineage_sources_disagree",
        )

    def test_public_hash_cache_is_invalidated_when_tip_changes(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        service = self._service()
        collector = CountingBlockHashCollector()
        service.public_block_hash = collector

        first = service._public_hash_at_height(
            100,
            observed_at=now,
            reference_tip=(100, "tip-a"),
        )
        cached = service._public_hash_at_height(
            100,
            observed_at=now,
            reference_tip=(100, "tip-a"),
        )
        refreshed = service._public_hash_at_height(
            100,
            observed_at=now,
            reference_tip=(100, "tip-b"),
        )

        self.assertEqual(collector.calls, 2)
        self.assertEqual(first.value["hash"], cached.value["hash"])
        self.assertNotEqual(
            cached.value["hash"], refreshed.value["hash"]
        )

    def test_missing_public_lineage_is_explicitly_degraded(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        recent_time = int((now - timedelta(minutes=5)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height": 100,
                "common_height_comparison_count": 2,
                "hashes_at_common_height": {
                    "node-a": "core-lineage",
                    "node-b": "core-lineage",
                },
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 100,
                        "best_hash": "core-lineage",
                        "block_time": recent_time,
                    },
                    {
                        "height": 100,
                        "best_hash": "core-lineage",
                        "block_time": recent_time,
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {
                "height": 102,
                "hash": "public-tip",
                "age_minutes": 5,
            },
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            observed_at=now,
            lag_threshold_blocks=3,
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )

        self.assertTrue(
            consistency.value[
                "public_core_lineage_comparison_incomplete"
            ]
        )
        self.assertEqual(assessment.level.value, "normal")
        self.assertEqual(
            assessment.data_quality, "core_public_lineage_unverified"
        )

    def test_old_core_quorum_is_scored_when_public_view_does_not_reconcile_it(
        self,
    ) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        old_time = int((now - timedelta(hours=6)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height": 100,
                "common_height_comparison_count": 2,
                "hashes_at_common_height": {
                    "node-a": "core-lineage",
                    "node-b": "core-lineage",
                },
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 100,
                        "best_hash": "core-lineage",
                        "block_time": old_time,
                    },
                    {
                        "height": 100,
                        "best_hash": "core-lineage",
                        "block_time": old_time,
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {
                "height": 102,
                "hash": "public-tip",
                "age_minutes": 5,
            },
            observed_at=now,
        )
        public_common_hash = Observation.ok(
            "public_common_height_hash",
            "test",
            {"height": 100, "hash": "core-lineage"},
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            public_common_hash,
            observed_at=now,
            lag_threshold_blocks=3,
            absolute_stale_minutes=180,
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )

        self.assertTrue(
            consistency.value["core_staleness_unresolved"]
        )
        self.assertEqual(assessment.level.value, "watch")
        self.assertEqual(assessment.evidence_score, 15)
        self.assertEqual(
            assessment.data_quality,
            "core_tip_age_not_publicly_reconciled",
        )

    def test_network_wide_old_tip_is_left_to_block_timing_rule(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        old_time = int((now - timedelta(hours=6)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height": 100,
                "common_height_comparison_count": 2,
                "hashes_at_common_height": {
                    "node-a": "shared-tip",
                    "node-b": "shared-tip",
                },
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 100,
                        "best_hash": "shared-tip",
                        "block_time": old_time,
                    },
                    {
                        "height": 100,
                        "best_hash": "shared-tip",
                        "block_time": old_time,
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {
                "height": 100,
                "hash": "shared-tip",
                "age_minutes": 360,
            },
            observed_at=now,
        )
        public_common_hash = Observation.ok(
            "public_common_height_hash",
            "test",
            {"height": 100, "hash": "shared-tip"},
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            public_common_hash,
            observed_at=now,
            absolute_stale_minutes=180,
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )

        self.assertTrue(
            consistency.value["public_confirms_network_wide_old_tip"]
        )
        self.assertFalse(
            consistency.value["core_staleness_unresolved"]
        )
        self.assertEqual(assessment.level.value, "normal")
        self.assertEqual(assessment.evidence_score, 0)

    def test_future_header_time_is_deduplicated_and_not_host_clock_blame(
        self,
    ) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        future_time = int((now + timedelta(minutes=5)).timestamp())
        chain = Observation.ok(
            "chain_signals",
            "test",
            {
                "quorum_met": True,
                "minimum_healthy_nodes": 2,
                "common_height": 100,
                "common_height_comparison_count": 2,
                "hashes_at_common_height": {
                    "node-a": "shared-tip",
                    "node-b": "shared-tip",
                },
                "max_reorg_depth": 0,
                "max_valid_fork_branch_length": 0,
                "node_divergence": False,
                "nodes": [
                    {
                        "height": 100,
                        "best_hash": "shared-tip",
                        "block_time": future_time,
                    },
                    {
                        "height": 100,
                        "best_hash": "shared-tip",
                        "block_time": future_time,
                    },
                ],
            },
            observed_at=now,
        )
        public_tip = Observation.ok(
            "public_chain_tip",
            "test",
            {
                "height": 100,
                "hash": "shared-tip",
                "age_minutes": 0,
            },
            observed_at=now,
        )
        public_common_hash = Observation.ok(
            "public_common_height_hash",
            "test",
            {"height": 100, "hash": "shared-tip"},
            observed_at=now,
        )

        consistency = compare_core_to_public_tip(
            chain,
            public_tip,
            public_common_hash,
            observed_at=now,
        )
        assessment = assess_risk(
            {
                "chain_signals": chain,
                "chain_public_consistency": consistency,
            }
        )

        self.assertEqual(
            consistency.value["future_header_timestamp_count"], 1
        )
        self.assertEqual(
            consistency.value["future_header_timestamp_node_count"], 2
        )
        self.assertEqual(
            assessment.data_quality, "future_block_header_timestamp"
        )
        self.assertFalse(
            any("host clocks" in action for action in assessment.actions)
        )


if __name__ == "__main__":
    unittest.main()
