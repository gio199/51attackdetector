from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ObservationStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    ERROR = "error"


class RiskLevel(str, Enum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    WATCH = "watch"
    WARNING = "warning"
    CRITICAL = "critical"


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class Observation:
    name: str
    source: str
    status: ObservationStatus
    observed_at: datetime = field(default_factory=utc_now)
    value: Any = None
    unit: str | None = None
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status in {ObservationStatus.OK, ObservationStatus.PARTIAL}

    @classmethod
    def ok(
        cls,
        name: str,
        source: str,
        value: Any,
        *,
        unit: str | None = None,
        observed_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
        partial: bool = False,
    ) -> "Observation":
        return cls(
            name=name,
            source=source,
            status=ObservationStatus.PARTIAL if partial else ObservationStatus.OK,
            observed_at=observed_at or utc_now(),
            value=value,
            unit=unit,
            metadata=metadata or {},
        )

    @classmethod
    def unavailable(
        cls,
        name: str,
        source: str,
        error: str,
        *,
        observed_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Observation":
        return cls(
            name=name,
            source=source,
            status=ObservationStatus.UNAVAILABLE,
            observed_at=observed_at or utc_now(),
            error=error,
            metadata=metadata or {},
        )

    @classmethod
    def failed(
        cls,
        name: str,
        source: str,
        error: str,
        *,
        observed_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Observation":
        return cls(
            name=name,
            source=source,
            status=ObservationStatus.ERROR,
            observed_at=observed_at or utc_now(),
            error=error,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class ScoreComponent:
    signal: str
    category: str
    points: int
    rule: str
    detail: str


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    evidence_score: int
    summary: str
    reasons: tuple[str, ...]
    actions: tuple[str, ...]
    data_quality: str
    confirmation_multiplier: float | None
    pause_settlement: bool
    score_components: tuple[ScoreComponent, ...] = ()
    observed_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        result = _json_value(asdict(self))
        result["score_note"] = (
            "Rule-based evidence score; it is not a calibrated probability of attack."
        )
        return result
