from __future__ import annotations

import unittest

from block_detector.models import Observation, ObservationStatus


class ObservationTests(unittest.TestCase):
    def test_unavailable_is_not_zero(self) -> None:
        observation = Observation.unavailable("hashrate", "test", "timeout")
        self.assertIsNone(observation.value)
        self.assertFalse(observation.available)
        self.assertEqual(observation.status, ObservationStatus.UNAVAILABLE)
        serialized = observation.to_dict()
        self.assertIsNone(serialized["value"])
        self.assertEqual(serialized["error"], "timeout")

    def test_partial_is_available_but_explicit(self) -> None:
        observation = Observation.ok(
            "nodes", "test", {"healthy": 1}, partial=True
        )
        self.assertTrue(observation.available)
        self.assertEqual(observation.status, ObservationStatus.PARTIAL)


if __name__ == "__main__":
    unittest.main()
