from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from .chain import ChainMonitor, build_chain_monitor
from .collectors import (
    BCHContextCollector,
    BlackoutNewsCollector,
    HashrateCollector,
    NiceHashContextCollector,
    PoolDistributionCollector,
    PublicBlockHashCollector,
    PublicChainCollector,
    RecentBlocksCollector,
    collect_research_context,
)
from .http import JsonHttpClient
from .models import Observation, RiskAssessment, RiskLevel, utc_now
from .market import MarketContextCollector
from .policy import assess_risk
from .settings import Settings
from .statistics import assess_block_timing


CollectorCall = Callable[[], Observation]


@dataclass
class _CacheEntry:
    stored_at: float
    observation: Observation


def compare_core_to_public_tip(
    chain_observation: Observation,
    public_tip_observation: Observation,
    public_common_hash_observation: Observation | None = None,
    *,
    observed_at: datetime,
    lag_threshold_blocks: int = 3,
    absolute_stale_minutes: float = 180.0,
) -> Observation:
    threshold = max(1, int(lag_threshold_blocks))
    stale_threshold = max(1.0, float(absolute_stale_minutes))
    source = "bitcoin-core+blockchain.info+mempool.space"
    if (
        not chain_observation.available
        or not isinstance(chain_observation.value, Mapping)
    ):
        return Observation.unavailable(
            "chain_public_consistency",
            source,
            "Bitcoin Core chain signals are unavailable",
            observed_at=observed_at,
        )

    try:
        chain_value = chain_observation.value

        def nonempty_hash(value: object) -> str | None:
            if value is None:
                return None
            result = str(value).strip()
            return result or None

        required_quorum = max(
            1, int(chain_value.get("minimum_healthy_nodes", 1) or 1)
        )
        nodes = chain_value.get("nodes", [])
        core_tips: list[tuple[int, str | None]] = []
        ages: list[float] = []
        future_header_node_count = 0
        future_header_tokens: set[str] = set()
        reference_time = observed_at
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)
        else:
            reference_time = reference_time.astimezone(timezone.utc)
        if isinstance(nodes, list):
            for item in nodes:
                if not isinstance(item, Mapping):
                    continue
                height: int | None = None
                if item.get("height") is not None:
                    try:
                        height = int(item["height"])
                    except (TypeError, ValueError):
                        height = None
                best_hash = nonempty_hash(item.get("best_hash"))
                if height is not None:
                    core_tips.append((height, best_hash))
                if item.get("block_time") is not None:
                    try:
                        block_time = datetime.fromtimestamp(
                            float(item["block_time"]), tz=timezone.utc
                        )
                        age = (
                            reference_time - block_time
                        ).total_seconds() / 60.0
                        if age < -2:
                            future_header_node_count += 1
                            future_header_tokens.add(
                                best_hash
                                or f"timestamp:{float(item['block_time'])}"
                            )
                        ages.append(max(0.0, age))
                    except (OSError, OverflowError, TypeError, ValueError):
                        continue
        if not core_tips and chain_value.get("common_height") is not None:
            core_tips.append((int(chain_value["common_height"]), None))

        heights = [height for height, _ in core_tips]
        absolute_stale_count = sum(
            age >= stale_threshold for age in ages
        )
        absolute_fresh_count = sum(age < stale_threshold for age in ages)
        core_quorum_extremely_stale = (
            len(ages) >= required_quorum
            and absolute_fresh_count < required_quorum
        )

        public_value = (
            public_tip_observation.value
            if public_tip_observation.available
            and isinstance(public_tip_observation.value, Mapping)
            else None
        )
        public_height: int | None = None
        public_tip_age: float | None = None
        public_tip_hash: str | None = None
        if public_value is not None:
            try:
                public_height = int(public_value["height"])
            except (KeyError, TypeError, ValueError):
                public_height = None
            try:
                raw_public_age = float(public_value["age_minutes"])
                public_tip_age = max(0.0, raw_public_age)
            except (KeyError, TypeError, ValueError):
                public_tip_age = None
            public_tip_hash = nonempty_hash(public_value.get("hash"))

        common_height: int | None = None
        try:
            if chain_value.get("common_height") is not None:
                common_height = int(chain_value["common_height"])
        except (TypeError, ValueError):
            common_height = None
        hashes_at_common_height = chain_value.get(
            "hashes_at_common_height", {}
        )
        common_core_hashes = (
            [
                hash_value
                for value in hashes_at_common_height.values()
                if (hash_value := nonempty_hash(value)) is not None
            ]
            if isinstance(hashes_at_common_height, Mapping)
            else []
        )

        lineage_height: int | None = None
        lineage_public_hash: str | None = None
        lineage_source_status = "not_requested"
        lineage_source_kind = "none"
        candidate_height: int | None = None
        candidate_hash: str | None = None
        common_public_value = (
            public_common_hash_observation.value
            if public_common_hash_observation is not None
            and public_common_hash_observation.available
            and isinstance(public_common_hash_observation.value, Mapping)
            else None
        )
        if public_common_hash_observation is not None:
            lineage_source_status = (
                public_common_hash_observation.status.value
            )
        if common_public_value is not None:
            try:
                candidate_height = int(common_public_value["height"])
            except (KeyError, TypeError, ValueError):
                candidate_height = None
            candidate_hash = nonempty_hash(common_public_value.get("hash"))
        candidate_is_common_height = bool(
            candidate_height is not None
            and candidate_hash is not None
            and candidate_height == common_height
        )
        public_lineage_sources_disagree = bool(
            candidate_is_common_height
            and public_height == common_height
            and public_tip_hash is not None
            and candidate_hash != public_tip_hash
        )

        if (
            public_height is not None
            and public_height == common_height
            and public_tip_hash is not None
        ):
            lineage_height = public_height
            lineage_public_hash = public_tip_hash
            lineage_source_status = public_tip_observation.status.value
            lineage_source_kind = "blockchain.info/latestblock"
        elif candidate_is_common_height:
            lineage_height = candidate_height
            lineage_public_hash = candidate_hash
            lineage_source_kind = "mempool.space/api/blocks"
        elif public_height is not None and public_tip_hash is not None:
            lineage_height = public_height
            lineage_public_hash = public_tip_hash
            lineage_source_status = public_tip_observation.status.value
            lineage_source_kind = "blockchain.info/latestblock"

        lineage_core_hashes: list[str] = []
        if (
            lineage_height is not None
            and lineage_height == common_height
            and common_core_hashes
        ):
            lineage_core_hashes = common_core_hashes
        elif lineage_height is not None:
            lineage_core_hashes = [
                best_hash
                for height, best_hash in core_tips
                if height == lineage_height and best_hash is not None
            ]

        lineage_comparison_count = (
            len(lineage_core_hashes)
            if lineage_public_hash is not None
            else 0
        )
        lineage_match_count = sum(
            core_hash == lineage_public_hash
            for core_hash in lineage_core_hashes
        )
        lineage_mismatch_count = sum(
            core_hash != lineage_public_hash
            for core_hash in lineage_core_hashes
        )
        lineage_comparison_complete = (
            lineage_public_hash is not None
            and lineage_comparison_count >= required_quorum
        )
        lineage_comparison_incomplete = not lineage_comparison_complete

        enough_core_nodes = len(core_tips) >= required_quorum
        gaps = (
            [public_height - height for height in heights]
            if public_height is not None
            else []
        )
        public_height_fresh_count = (
            sum(gap < threshold for gap in gaps) if gaps else None
        )
        lagging_core_count = (
            sum(gap >= threshold for gap in gaps) if gaps else None
        )
        core_ahead_public_count = (
            sum(gap <= -threshold for gap in gaps) if gaps else None
        )
        fresh_quorum_met = (
            enough_core_nodes
            and public_height_fresh_count is not None
            and public_height_fresh_count >= required_quorum
        )
        core_quorum_behind = bool(
            enough_core_nodes
            and lagging_core_count
            and not fresh_quorum_met
        )
        core_ahead_public_quorum_met = bool(
            core_ahead_public_count is not None
            and core_ahead_public_count >= required_quorum
        )
        public_tip_also_extremely_old = bool(
            public_tip_age is not None
            and public_tip_age >= stale_threshold
        )
        public_confirms_network_wide_old_tip = bool(
            core_quorum_extremely_stale
            and public_tip_also_extremely_old
            and public_height is not None
            and heights
            and all(height == public_height for height in heights)
            and lineage_comparison_complete
            and lineage_mismatch_count == 0
            and not public_lineage_sources_disagree
        )
        core_staleness_unresolved = bool(
            core_quorum_extremely_stale
            and not public_confirms_network_wide_old_tip
        )

        if not heights and not ages:
            raise ValueError(
                "Bitcoin Core snapshot contains no comparable heights or tip times"
            )

        value: dict[str, object] = {
            "public_reference_available": public_height is not None,
            "public_height": public_height,
            "public_tip_age_minutes": public_tip_age,
            "highest_core_height": max(heights) if heights else None,
            "lowest_core_height": min(heights) if heights else None,
            "lag_threshold_blocks": threshold,
            "required_core_quorum": required_quorum,
            "compared_core_node_count": len(core_tips),
            "maximum_public_minus_core_blocks": (
                max(gaps) if gaps else None
            ),
            "minimum_public_minus_core_blocks": (
                min(gaps) if gaps else None
            ),
            "public_height_fresh_node_count": (
                public_height_fresh_count
            ),
            "public_height_fresh_quorum_met": fresh_quorum_met,
            "public_lagging_core_node_count": lagging_core_count,
            "core_quorum_materially_behind_public": core_quorum_behind,
            "core_ahead_public_node_count": core_ahead_public_count,
            "core_ahead_public_quorum_met": (
                core_ahead_public_quorum_met
            ),
            "public_reference_materially_behind_core": (
                core_ahead_public_quorum_met
            ),
            "lineage_height": lineage_height,
            "lineage_source_status": lineage_source_status,
            "lineage_source_kind": lineage_source_kind,
            "public_lineage_sources_disagree": (
                public_lineage_sources_disagree
            ),
            "lineage_hash_comparison_count": (
                lineage_comparison_count
            ),
            "lineage_hash_match_count": lineage_match_count,
            "lineage_hash_mismatch_count": lineage_mismatch_count,
            "public_core_lineage_mismatch": (
                lineage_mismatch_count > 0
            ),
            "public_core_lineage_comparison_complete": (
                lineage_comparison_complete
            ),
            "public_core_lineage_comparison_incomplete": (
                lineage_comparison_incomplete
            ),
            "oldest_core_tip_age_minutes": max(ages) if ages else None,
            "newest_core_tip_age_minutes": min(ages) if ages else None,
            "core_tip_age_comparison_count": len(ages),
            "absolute_stale_threshold_minutes": stale_threshold,
            "absolute_stale_core_node_count": absolute_stale_count,
            "absolute_fresh_core_node_count": absolute_fresh_count,
            "core_quorum_extremely_stale": (
                core_quorum_extremely_stale
            ),
            "public_tip_also_extremely_old": (
                public_tip_also_extremely_old
            ),
            "public_confirms_network_wide_old_tip": (
                public_confirms_network_wide_old_tip
            ),
            "core_staleness_unresolved": core_staleness_unresolved,
            "future_header_timestamp_count": len(
                future_header_tokens
            ),
            "future_header_timestamp_node_count": (
                future_header_node_count
            ),
        }
        partial = (
            public_height is None
            or bool(lagging_core_count)
            or bool(core_ahead_public_count)
            or lineage_mismatch_count > 0
            or public_lineage_sources_disagree
            or lineage_comparison_incomplete
            or core_staleness_unresolved
            or future_header_node_count > 0
            or not enough_core_nodes
        )
        return Observation.ok(
            "chain_public_consistency",
            source,
            value,
            observed_at=observed_at,
            partial=partial,
            metadata={
                "description": (
                    "Independent public height and common-height lineage "
                    "cross-checks plus a conservative tip-header age fallback; "
                    "none is attack proof."
                ),
                "public_height_reference_status": (
                    public_tip_observation.status.value
                ),
                "public_lineage_reference_status": (
                    lineage_source_status
                ),
            },
        )
    except Exception as exc:
        return Observation.failed(
            "chain_public_consistency",
            source,
            str(exc),
            observed_at=observed_at,
        )


class MonitorService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        public_chain: PublicChainCollector | None = None,
        public_block_hash: PublicBlockHashCollector | None = None,
        recent_blocks: RecentBlocksCollector | None = None,
        hashrate: HashrateCollector | None = None,
        pools: PoolDistributionCollector | None = None,
        blackouts: BlackoutNewsCollector | None = None,
        bitcoin_cash: BCHContextCollector | None = None,
        nicehash: NiceHashContextCollector | None = None,
        market: MarketContextCollector | None = None,
        chain_monitor: ChainMonitor | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or Settings.from_env()
        def http(
            timeout: tuple[float, float] | None = None,
            *,
            retries: int = 3,
        ) -> JsonHttpClient:
            return JsonHttpClient(
                timeout=timeout or self.settings.request_timeout,
                retries=retries,
            )

        self.public_chain = public_chain or PublicChainCollector(http())
        self.public_block_hash = (
            public_block_hash or PublicBlockHashCollector(http())
        )
        self.recent_blocks = recent_blocks or RecentBlocksCollector(
            http(), limit=self.settings.block_history_limit
        )
        self.hashrate = hashrate or HashrateCollector(http())
        self.pools = pools or PoolDistributionCollector(http())
        self.blackouts = blackouts or BlackoutNewsCollector(
            self.settings.news_api_key,
            http(
                (
                    max(5.0, self.settings.request_connect_timeout),
                    max(15.0, self.settings.request_read_timeout),
                ),
                retries=0,
            ),
        )
        self.bitcoin_cash = bitcoin_cash or BCHContextCollector(
            http(), api_key=self.settings.blockchair_api_key
        )
        self.nicehash = nicehash or NiceHashContextCollector(http())
        self.market = market or MarketContextCollector(http())
        self.chain_monitor = chain_monitor or build_chain_monitor(self.settings)
        self.monotonic = monotonic
        self._cache: dict[str, _CacheEntry] = {}
        self._public_hash_cache_height: int | None = None
        self._public_hash_cache_reference_tip: (
            tuple[int, str] | None
        ) = None

    def _cached(
        self,
        name: str,
        ttl_seconds: float,
        call: CollectorCall,
    ) -> Observation:
        current = self.monotonic()
        cached = self._cache.get(name)
        if cached and current - cached.stored_at < ttl_seconds:
            return cached.observation
        observation = call()
        self._cache[name] = _CacheEntry(current, observation)
        return observation

    def _public_hash_at_height(
        self,
        height: int,
        *,
        observed_at: datetime,
        reference_tip: tuple[int, str] | None,
    ) -> Observation:
        name = "public_common_height_hash"
        current = self.monotonic()
        cached = self._cache.get(name)
        if (
            cached
            and self._public_hash_cache_height == height
            and self._public_hash_cache_reference_tip == reference_tip
            and current - cached.stored_at < 30
        ):
            return cached.observation
        observation = self._safe(
            name,
            "mempool.space/api/blocks",
            lambda: self.public_block_hash.collect(
                height=height,
                now=observed_at,
            ),
        )
        self._cache[name] = _CacheEntry(current, observation)
        self._public_hash_cache_height = height
        self._public_hash_cache_reference_tip = reference_tip
        return observation

    @staticmethod
    def _safe(name: str, source: str, call: CollectorCall) -> Observation:
        try:
            return call()
        except Exception as exc:
            return Observation.failed(name, source, str(exc))

    def collect(self, *, now: datetime | None = None) -> dict[str, object]:
        observed_at = now or utc_now()
        jobs: dict[str, CollectorCall] = {
            "public_chain_tip": lambda: self._safe(
                "public_chain_tip",
                "blockchain.info/latestblock",
                lambda: self.public_chain.collect(now=observed_at),
            ),
            "recent_block_intervals": lambda: self._cached(
                "recent_block_intervals",
                300,
                lambda: self._safe(
                    "recent_block_intervals",
                    "blockchair.com/bitcoin/blocks",
                    lambda: self.recent_blocks.collect(now=observed_at),
                ),
            ),
            "network_hashrate": lambda: self._cached(
                "network_hashrate",
                1800,
                lambda: self._safe(
                    "network_hashrate",
                    "blockchain.com/charts/hash-rate",
                    lambda: self.hashrate.collect(now=observed_at),
                ),
            ),
            "mining_pool_distribution": lambda: self._cached(
                "mining_pool_distribution",
                3600,
                lambda: self._safe(
                    "mining_pool_distribution",
                    "blockchain.com/pools",
                    lambda: self.pools.collect(now=observed_at),
                ),
            ),
            "blackout_news": lambda: self._cached(
                "blackout_news",
                3600,
                lambda: self._safe(
                    "blackout_news",
                    "newsapi.org",
                    lambda: self.blackouts.collect(now=observed_at),
                ),
            ),
            "bitcoin_cash_context": lambda: self._cached(
                "bitcoin_cash_context",
                900,
                lambda: self._safe(
                    "bitcoin_cash_context",
                    "blockchair.com/bitcoin-cash/stats",
                    lambda: self.bitcoin_cash.collect(now=observed_at),
                ),
            ),
            "nicehash_sha256_context": lambda: self._cached(
                "nicehash_sha256_context",
                900,
                lambda: self._safe(
                    "nicehash_sha256_context",
                    "api2.nicehash.com/public",
                    lambda: self.nicehash.collect(now=observed_at),
                ),
            ),
            "market_context": lambda: self._cached(
                "market_context",
                300,
                lambda: self._safe(
                    "market_context",
                    "binance/bitfinex/bybit/okx",
                    lambda: self.market.collect(now=observed_at),
                ),
            ),
            "chain_signals": lambda: self._safe(
                "chain_signals", "bitcoin-core", self.chain_monitor.collect
            ),
        }

        observations: dict[str, Observation] = {}
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = {executor.submit(call): name for name, call in jobs.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    observations[name] = future.result()
                except Exception as exc:  # defensive boundary around worker failures
                    observations[name] = Observation.failed(
                        name, "orchestrator", str(exc)
                    )

        chain_for_lineage = observations["chain_signals"]
        chain_for_lineage_value = (
            chain_for_lineage.value
            if chain_for_lineage.available
            and isinstance(chain_for_lineage.value, Mapping)
            else {}
        )
        try:
            common_height_value = chain_for_lineage_value.get(
                "common_height"
            )
            common_height = (
                int(common_height_value)
                if common_height_value is not None
                else None
            )
        except (TypeError, ValueError):
            common_height = None
        if common_height is None:
            observations["public_common_height_hash"] = (
                Observation.unavailable(
                    "public_common_height_hash",
                    "mempool.space/api/blocks",
                    "Bitcoin Core common comparison height is unavailable",
                    observed_at=observed_at,
                )
            )
        else:
            public_tip_for_cache = observations["public_chain_tip"]
            public_tip_for_cache_value = (
                public_tip_for_cache.value
                if public_tip_for_cache.available
                and isinstance(public_tip_for_cache.value, Mapping)
                else {}
            )
            try:
                cache_tip_height = int(
                    public_tip_for_cache_value["height"]
                )
                cache_tip_hash = str(
                    public_tip_for_cache_value["hash"]
                ).strip()
                reference_tip = (
                    (cache_tip_height, cache_tip_hash)
                    if cache_tip_hash
                    else None
                )
            except (KeyError, TypeError, ValueError):
                reference_tip = None
            observations["public_common_height_hash"] = (
                self._public_hash_at_height(
                    common_height,
                    observed_at=observed_at,
                    reference_tip=reference_tip,
                )
            )

        public_tip = observations["public_chain_tip"]
        intervals = observations["recent_block_intervals"]
        if public_tip.available and isinstance(public_tip.value, Mapping):
            age = float(public_tip.value["age_minutes"])
            recent_values: list[float] = []
            if intervals.available and isinstance(intervals.value, Mapping):
                raw_intervals = intervals.value.get("intervals_minutes", [])
                if isinstance(raw_intervals, list):
                    recent_values = [float(item) for item in raw_intervals]
            timing = assess_block_timing(
                age,
                recent_values,
                expected_minutes=self.settings.expected_block_minutes,
                watch_tail=self.settings.timing_watch_tail,
                warning_tail=self.settings.timing_warning_tail,
                critical_tail=self.settings.timing_critical_tail,
            )
            observations["block_timing"] = Observation.ok(
                "block_timing",
                "local-statistical-policy",
                timing.to_dict(),
                observed_at=observed_at,
                partial=not intervals.available,
            )
            height = int(public_tip.value["height"])
        else:
            observations["block_timing"] = Observation.unavailable(
                "block_timing",
                "local-statistical-policy",
                "Current public chain tip is unavailable",
                observed_at=observed_at,
            )
            height = None

        observations["research_context"] = collect_research_context(
            height, now=observed_at
        )
        observations["chain_public_consistency"] = compare_core_to_public_tip(
            observations["chain_signals"],
            observations["public_chain_tip"],
            observations["public_common_height_hash"],
            observed_at=observed_at,
            lag_threshold_blocks=self.settings.public_height_lag_blocks,
            absolute_stale_minutes=(
                self.settings.core_absolute_stale_minutes
            ),
        )
        assessment = assess_risk(observations)
        return {
            "schema_version": 1,
            "generated_at": observed_at.isoformat(),
            "assessment": assessment.to_dict(),
            "observations": {
                name: observation.to_dict()
                for name, observation in sorted(observations.items())
            },
        }


class AlertGate:
    """Suppress repeated alerts until evidence changes or a repeat interval elapses."""

    def __init__(
        self,
        repeat_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repeat_seconds = repeat_seconds
        self.monotonic = monotonic
        self._last_fingerprint: str | None = None
        self._last_emitted_at: float | None = None

    def should_emit(self, snapshot: Mapping[str, object]) -> bool:
        assessment = snapshot.get("assessment")
        if not isinstance(assessment, Mapping):
            return False
        level = str(assessment.get("level", RiskLevel.UNKNOWN.value))
        if level not in {
            RiskLevel.WATCH.value,
            RiskLevel.WARNING.value,
            RiskLevel.CRITICAL.value,
        }:
            self._last_fingerprint = None
            self._last_emitted_at = None
            return False

        observations = snapshot.get("observations")
        public_hash = None
        if isinstance(observations, Mapping):
            public_tip = observations.get("public_chain_tip")
            if isinstance(public_tip, Mapping):
                value = public_tip.get("value")
                if isinstance(value, Mapping):
                    public_hash = value.get("hash")
        fingerprint = json.dumps(
            {
                "level": level,
                "reasons": assessment.get("reasons"),
                "public_hash": public_hash,
            },
            sort_keys=True,
        )
        current = self.monotonic()
        evidence_changed = fingerprint != self._last_fingerprint
        repeat_due = (
            self._last_emitted_at is None
            or current - self._last_emitted_at >= self.repeat_seconds
        )
        if evidence_changed or repeat_due:
            self._last_fingerprint = fingerprint
            self._last_emitted_at = current
            return True
        return False
