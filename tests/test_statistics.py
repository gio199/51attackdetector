from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from block_detector.models import RiskLevel
from block_detector.statistics import (
    assess_block_timing,
    average_interval_tail_probability,
    block_age_tail_probability,
    intervals_from_blocks,
)


class BlockTimingTests(unittest.TestCase):
    def test_ten_minutes_is_not_an_alert(self) -> None:
        assessment = assess_block_timing(10.0)
        self.assertEqual(assessment.level, RiskLevel.NORMAL)
        self.assertAlmostEqual(assessment.age_tail_probability, math.exp(-1))

    def test_statistical_tail_boundaries(self) -> None:
        self.assertEqual(assess_block_timing(30.0).level, RiskLevel.WATCH)
        self.assertEqual(assess_block_timing(50.0).level, RiskLevel.WARNING)
        self.assertEqual(assess_block_timing(70.0).level, RiskLevel.CRITICAL)

    def test_negative_age_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            block_age_tail_probability(-0.1)

    def test_recent_mean_uses_erlang_tail(self) -> None:
        probability = average_interval_tail_probability(20.0, 9)
        self.assertLess(probability, 0.01)
        assessment = assess_block_timing(1.0, [20.0] * 9)
        self.assertIn(assessment.level, {RiskLevel.WARNING, RiskLevel.CRITICAL})

    def test_intervals_require_consecutive_heights(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        blocks = [
            (100, now),
            (99, now - timedelta(minutes=8)),
            (97, now - timedelta(minutes=30)),
            (96, now - timedelta(minutes=41)),
        ]
        self.assertEqual(intervals_from_blocks(blocks), [8.0, 11.0])


if __name__ == "__main__":
    unittest.main()
