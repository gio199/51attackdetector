from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .models import RiskLevel


def block_age_tail_probability(
    age_minutes: float, expected_minutes: float = 10.0
) -> float:
    """Probability that a normal exponential block interval lasts at least this long."""
    if expected_minutes <= 0:
        raise ValueError("expected_minutes must be positive")
    if age_minutes < 0:
        raise ValueError("age_minutes cannot be negative")
    return math.exp(-age_minutes / expected_minutes)


def average_interval_tail_probability(
    average_minutes: float,
    interval_count: int,
    expected_minutes: float = 10.0,
) -> float:
    """Erlang upper-tail probability for the mean of exponential intervals."""
    if expected_minutes <= 0:
        raise ValueError("expected_minutes must be positive")
    if average_minutes < 0:
        raise ValueError("average_minutes cannot be negative")
    if interval_count < 1:
        raise ValueError("interval_count must be at least one")

    scaled_sum = interval_count * average_minutes / expected_minutes
    if scaled_sum > 745:
        return 0.0

    term = 1.0
    series = 1.0
    for index in range(1, interval_count):
        term *= scaled_sum / index
        series += term
    return min(1.0, math.exp(-scaled_sum) * series)


def _level_for_probability(
    probability: float,
    *,
    watch_tail: float,
    warning_tail: float,
    critical_tail: float,
) -> RiskLevel:
    if probability <= critical_tail:
        return RiskLevel.CRITICAL
    if probability <= warning_tail:
        return RiskLevel.WARNING
    if probability <= watch_tail:
        return RiskLevel.WATCH
    return RiskLevel.NORMAL


@dataclass(frozen=True)
class BlockTimingAssessment:
    level: RiskLevel
    age_minutes: float
    age_tail_probability: float
    recent_average_minutes: float | None
    recent_interval_count: int
    recent_average_tail_probability: float | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["level"] = self.level.value
        return result


def assess_block_timing(
    age_minutes: float,
    recent_intervals_minutes: Sequence[float] = (),
    *,
    expected_minutes: float = 10.0,
    watch_tail: float = 0.05,
    warning_tail: float = 0.01,
    critical_tail: float = 0.001,
) -> BlockTimingAssessment:
    if not 0 < critical_tail < warning_tail < watch_tail < 1:
        raise ValueError("tail thresholds must satisfy critical < warning < watch < 1")

    age_probability = block_age_tail_probability(age_minutes, expected_minutes)
    age_level = _level_for_probability(
        age_probability,
        watch_tail=watch_tail,
        warning_tail=warning_tail,
        critical_tail=critical_tail,
    )

    clean_intervals = [
        float(value) for value in recent_intervals_minutes if float(value) >= 0
    ]
    average_minutes: float | None = None
    average_probability: float | None = None
    average_level = RiskLevel.NORMAL
    if len(clean_intervals) >= 3:
        average_minutes = statistics.fmean(clean_intervals)
        average_probability = average_interval_tail_probability(
            average_minutes, len(clean_intervals), expected_minutes
        )
        average_level = _level_for_probability(
            average_probability,
            watch_tail=watch_tail,
            warning_tail=warning_tail,
            critical_tail=critical_tail,
        )

    levels = {
        RiskLevel.NORMAL: 0,
        RiskLevel.WATCH: 1,
        RiskLevel.WARNING: 2,
        RiskLevel.CRITICAL: 3,
    }
    level = max((age_level, average_level), key=levels.__getitem__)
    if level is RiskLevel.NORMAL:
        reason = "Block timing is within the configured statistical range."
    elif age_level == level and average_level == level:
        reason = "Both the current wait and recent interval mean are unusually long."
    elif age_level == level:
        reason = "The current block wait is statistically unusual, but is not attack proof."
    else:
        reason = "The recent mean block interval is statistically unusual."

    return BlockTimingAssessment(
        level=level,
        age_minutes=age_minutes,
        age_tail_probability=age_probability,
        recent_average_minutes=average_minutes,
        recent_interval_count=len(clean_intervals),
        recent_average_tail_probability=average_probability,
        reason=reason,
    )


def intervals_from_blocks(
    blocks: Iterable[tuple[int, datetime]],
) -> list[float]:
    """Return minutes between consecutive heights, ignoring gaps and invalid times."""
    ordered = sorted(blocks, key=lambda item: item[0], reverse=True)
    intervals: list[float] = []
    for current, previous in zip(ordered, ordered[1:]):
        current_height, current_time = current
        previous_height, previous_time = previous
        if current_height - previous_height != 1:
            continue
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if previous_time.tzinfo is None:
            previous_time = previous_time.replace(tzinfo=timezone.utc)
        difference = (current_time - previous_time).total_seconds() / 60.0
        if difference >= 0:
            intervals.append(difference)
    return intervals
