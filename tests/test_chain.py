from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from block_detector.chain import (
    BitcoinRPCClient,
    ChainMonitor,
    ChainStateStore,
    ChainTip,
    NodeSnapshot,
    PreviousTip,
)
from block_detector.models import RiskLevel
from block_detector.policy import assess_risk


def snapshot(
    node_id: str,
    *,
    height: int = 100,
    block_hash: str = "a",
    previous_hash: str = "p",
    chainwork: int = 1000,
    tips: tuple[ChainTip, ...] = (),
) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chain="main",
        height=height,
        headers=height,
        best_hash=block_hash,
        previous_hash=previous_hash,
        block_time=1_767_225_600,
        chainwork=chainwork,
        initial_block_download=False,
        verification_progress=1.0,
        pruned=False,
        warnings="",
        tips=tips,
    )


class FakeRPC:
    def __init__(
        self,
        item: NodeSnapshot | Exception,
        *,
        common_hash: str | None = None,
        detached_depth: int | None = 0,
        wallet_result=None,
    ) -> None:
        self.item = item
        self.node_id = (
            item.node_id if isinstance(item, NodeSnapshot) else "failed-node"
        )
        self.common_hash = common_hash
        self.detached_depth = detached_depth
        self.wallet_result = wallet_result

    def snapshot(self):
        if isinstance(self.item, Exception):
            raise self.item
        return self.item

    def active_hash_at_height(self, height: int) -> str:
        return self.common_hash or f"common-{height}"

    def find_detached_depth(self, previous, current, *, max_depth: int):
        return self.detached_depth

    def call(self, method: str, params=()):
        if method == "listsinceblock" and self.wallet_result is not None:
            return self.wallet_result
        raise AssertionError(f"unexpected RPC call: {method}")


class ChainMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = ChainStateStore(Path(self.temp.name) / "state.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_no_clients_is_unavailable(self) -> None:
        observation = ChainMonitor([], state_store=self.state).collect()
        self.assertFalse(observation.available)
        self.assertIsNone(observation.value)

    def test_quorum_and_normal_lag_share_common_hash(self) -> None:
        first = FakeRPC(snapshot("one", height=101, block_hash="tip-101"))
        second = FakeRPC(snapshot("two", height=100, block_hash="tip-100"))
        observation = ChainMonitor(
            [first, second],
            state_store=self.state,
            minimum_healthy_nodes=2,
        ).collect()
        self.assertTrue(observation.value["quorum_met"])
        self.assertFalse(observation.value["node_divergence"])
        self.assertEqual(observation.value["height_spread"], 1)

    def test_common_height_hash_mismatch_is_divergence(self) -> None:
        first = FakeRPC(snapshot("one"), common_hash="hash-a")
        second = FakeRPC(snapshot("two"), common_hash="hash-b")
        observation = ChainMonitor(
            [first, second],
            state_store=self.state,
            minimum_healthy_nodes=2,
        ).collect()
        self.assertTrue(observation.value["node_divergence"])

    def test_failed_common_height_comparison_reduces_quorum(self) -> None:
        class BrokenComparisonRPC(FakeRPC):
            def active_hash_at_height(self, height: int) -> str:
                raise RuntimeError("comparison failed")

        observation = ChainMonitor(
            [
                FakeRPC(snapshot("one")),
                BrokenComparisonRPC(snapshot("two")),
            ],
            state_store=self.state,
            minimum_healthy_nodes=2,
        ).collect()
        self.assertFalse(observation.value["quorum_met"])
        self.assertEqual(
            observation.value["common_height_comparison_count"], 1
        )
        self.assertEqual(observation.status.value, "partial")

    def test_two_block_reorg_is_recorded(self) -> None:
        old = snapshot("one", height=100, block_hash="old", chainwork=1000)
        self.state.save([old])
        current = snapshot(
            "one", height=101, block_hash="new", previous_hash="other", chainwork=1100
        )
        observation = ChainMonitor(
            [FakeRPC(current, detached_depth=2)],
            state_store=self.state,
            minimum_healthy_nodes=1,
        ).collect()
        self.assertEqual(observation.value["max_reorg_depth"], 2)
        self.assertEqual(observation.value["reorgs"][0]["previous_hash"], "old")

    def test_failed_node_state_is_preserved(self) -> None:
        self.state.save([snapshot("one"), snapshot("failed-node")])
        healthy = FakeRPC(snapshot("one", height=101, block_hash="next"))
        failed = FakeRPC(RuntimeError("offline"))
        ChainMonitor(
            [healthy, failed],
            state_store=self.state,
            minimum_healthy_nodes=1,
        ).collect()
        self.assertIn("failed-node", self.state.load())

    def test_chain_quality_event_is_visible_and_affects_assessment(self) -> None:
        old = snapshot("one", block_hash="old", chainwork=1000)
        self.state.save([old])
        current = snapshot("one", block_hash="new", chainwork=900)
        observation = ChainMonitor(
            [FakeRPC(current, detached_depth=0)],
            state_store=self.state,
            minimum_healthy_nodes=1,
        ).collect()
        self.assertIn(
            "chainwork_not_increasing",
            observation.value["quality_event_kinds"],
        )
        self.assertEqual(observation.status.value, "partial")
        assessment = assess_risk({"chain_signals": observation})
        self.assertEqual(assessment.level, RiskLevel.WATCH)
        self.assertEqual(
            assessment.data_quality, "degraded_direct_chain_data"
        )
        preserved = self.state.load()["one"]
        self.assertEqual(preserved.best_hash, "old")
        self.assertEqual(preserved.chainwork, 1000)

    def test_unknown_depth_discontinuity_preserves_trusted_state(self) -> None:
        old = snapshot("one", block_hash="old", chainwork=1000)
        self.state.save([old])
        current = snapshot("one", block_hash="new", chainwork=1100)
        observation = ChainMonitor(
            [FakeRPC(current, detached_depth=None)],
            state_store=self.state,
            minimum_healthy_nodes=1,
        ).collect()
        self.assertIn(
            "tip_discontinuity_unknown_depth",
            observation.value["quality_event_kinds"],
        )
        preserved = self.state.load()["one"]
        self.assertEqual(preserved.best_hash, "old")
        self.assertEqual(preserved.chainwork, 1000)

    def test_wallet_removed_transaction_is_reported(self) -> None:
        current = snapshot("one")
        self.state.save([current], wallet_cursors={"one": "cursor"})
        wallet_result = {
            "removed": [
                {
                    "txid": "tx",
                    "category": "receive",
                    "amount": 1.0,
                    "confirmations": -1,
                    "walletconflicts": ["replacement"],
                }
            ],
            "lastblock": "next-cursor",
        }
        observation = ChainMonitor(
            [FakeRPC(current, wallet_result=wallet_result)],
            state_store=self.state,
            minimum_healthy_nodes=1,
            monitor_wallet_transactions=True,
        ).collect()
        self.assertEqual(observation.value["wallet_removed_at_risk_count"], 1)
        self.assertEqual(
            self.state.load_wallet_cursors()["one"], "next-cursor"
        )

    def test_reorg_depth_walks_to_common_ancestor(self) -> None:
        class WalkingClient(BitcoinRPCClient):
            def __init__(self):
                self.node_id = "one"

            def call(self, method, params=()):
                value = params[0]
                if method == "getblockhash":
                    return {
                        99: "common-99",
                        100: "new-100",
                        101: "new-101",
                    }[value]
                if method == "getblockheader":
                    return {
                        "old-100": {
                            "height": 100,
                            "previousblockhash": "common-99",
                        },
                        "common-99": {
                            "height": 99,
                            "previousblockhash": "common-98",
                        },
                    }[value]
                raise AssertionError(method)

        old = PreviousTip(
            node_id="one",
            observed_at="2026-01-01T00:00:00+00:00",
            height=100,
            best_hash="old-100",
            previous_hash="common-99",
            chainwork=1000,
        )
        current = snapshot(
            "one",
            height=101,
            block_hash="new-101",
            previous_hash="new-100",
            chainwork=1100,
        )
        depth = WalkingClient().find_detached_depth(
            old, current, max_depth=10
        )
        self.assertEqual(depth, 1)

    def test_skipped_poll_with_old_tip_still_active_is_not_reorg(self) -> None:
        class SkippedPollClient(BitcoinRPCClient):
            def __init__(self):
                self.node_id = "one"

            def active_hash_at_height(self, height: int) -> str:
                return "old-100"

        old = PreviousTip(
            node_id="one",
            observed_at="2026-01-01T00:00:00+00:00",
            height=100,
            best_hash="old-100",
            previous_hash="old-99",
            chainwork=1000,
        )
        current = snapshot(
            "one",
            height=103,
            block_hash="new-103",
            previous_hash="new-102",
            chainwork=1200,
        )
        self.assertEqual(
            SkippedPollClient().find_detached_depth(
                old, current, max_depth=10
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
