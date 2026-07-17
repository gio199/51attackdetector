from __future__ import annotations

from typing import Mapping

from .models import (
    Observation,
    ObservationStatus,
    RiskAssessment,
    RiskLevel,
    ScoreComponent,
)


def _value(observation: Observation | None) -> Mapping[str, object]:
    if observation is None or not observation.available:
        return {}
    if isinstance(observation.value, Mapping):
        return observation.value
    return {}


def assess_risk(observations: Mapping[str, Observation]) -> RiskAssessment:
    score = 0
    reasons: list[str] = []
    actions: list[str] = []
    components: list[ScoreComponent] = []

    def add_points(
        signal: str,
        category: str,
        configured_points: int,
        detail: str,
    ) -> None:
        nonlocal score
        applied_points = min(configured_points, max(0, 100 - score))
        score += applied_points
        components.append(
            ScoreComponent(
                signal=signal,
                category=category,
                points=applied_points,
                rule=f"add:{configured_points}",
                detail=detail,
            )
        )
        reasons.append(detail)

    def apply_floor(
        signal: str,
        category: str,
        minimum_score: int,
        detail: str,
    ) -> None:
        nonlocal score
        previous = score
        score = max(score, minimum_score)
        components.append(
            ScoreComponent(
                signal=signal,
                category=category,
                points=score - previous,
                rule=f"minimum:{minimum_score}",
                detail=detail,
            )
        )
        reasons.append(detail)

    chain_observation = observations.get("chain_signals")
    chain = _value(chain_observation)
    chain_available = bool(chain)
    quorum_met = bool(chain.get("quorum_met", False))
    comparison_count_value = chain.get("common_height_comparison_count")
    single_node_only = (
        isinstance(comparison_count_value, int)
        and comparison_count_value < 2
    )
    reorg_depth = int(chain.get("max_reorg_depth", 0) or 0)
    fork_depth = int(chain.get("max_valid_fork_branch_length", 0) or 0)
    node_divergence = bool(chain.get("node_divergence", False))
    wallet_removed_at_risk = int(
        chain.get("wallet_removed_at_risk_count", 0) or 0
    )
    quality_event_kinds = {
        str(item)
        for item in chain.get("quality_event_kinds", [])
        if isinstance(item, str)
    }
    consistency = _value(observations.get("chain_public_consistency"))
    core_lags_public = bool(
        consistency.get("core_quorum_materially_behind_public", False)
    )
    public_hash_mismatch = bool(
        consistency.get("public_core_lineage_mismatch", False)
    )
    public_lineage_sources_disagree = bool(
        consistency.get("public_lineage_sources_disagree", False)
    )
    public_source_disagreement_watch = (
        public_lineage_sources_disagree and not public_hash_mismatch
    )
    public_lineage_incomplete = bool(consistency) and bool(
        consistency.get(
            "public_core_lineage_comparison_incomplete", True
        )
    )
    public_reference_lags = bool(
        consistency.get("public_reference_materially_behind_core", False)
    )
    core_staleness_unresolved = bool(
        consistency.get("core_staleness_unresolved", False)
    )
    core_age_watch = core_staleness_unresolved and not core_lags_public
    future_header_timestamp_count = int(
        consistency.get("future_header_timestamp_count", 0) or 0
    )
    future_header_timestamp_node_count = int(
        consistency.get(
            "future_header_timestamp_node_count", 0
        )
        or 0
    )

    if reorg_depth >= 6:
        apply_floor(
            "chain_reorganization",
            "direct",
            100,
            f"A Bitcoin Core node observed a {reorg_depth}-block reorg.",
        )
    elif reorg_depth >= 3:
        apply_floor(
            "chain_reorganization",
            "direct",
            85,
            f"A Bitcoin Core node observed a {reorg_depth}-block reorg.",
        )
    elif reorg_depth == 2:
        apply_floor(
            "chain_reorganization",
            "direct",
            70,
            "A Bitcoin Core node observed a two-block reorg.",
        )
    elif reorg_depth == 1:
        add_points(
            "chain_reorganization",
            "direct",
            15,
            "A shallow one-block reorg was observed.",
        )

    if node_divergence:
        apply_floor(
            "node_divergence",
            "direct",
            60,
            "Healthy nodes disagree at their common chain height.",
        )
    if wallet_removed_at_risk:
        apply_floor(
            "wallet_removed_transactions",
            "direct",
            70,
            f"{wallet_removed_at_risk} wallet transaction(s) were removed and remain unconfirmed or conflicted.",
        )
    if fork_depth >= 3:
        apply_floor(
            "competing_branch",
            "direct",
            55,
            f"A validated competing branch is {fork_depth} blocks long.",
        )
    elif fork_depth == 2:
        add_points(
            "competing_branch",
            "direct",
            30,
            "A validated two-block competing branch is visible.",
        )
    elif fork_depth == 1:
        add_points(
            "competing_branch",
            "direct",
            5,
            "A one-block competing branch is visible.",
        )

    if "tip_discontinuity_unknown_depth" in quality_event_kinds:
        add_points(
            "tip_discontinuity",
            "direct",
            15,
            "A node tip changed and the detached depth could not be established.",
        )
    if "chainwork_not_increasing" in quality_event_kinds:
        add_points(
            "chainwork_progression",
            "direct",
            15,
            "A node tip changed without an increase in reported chainwork.",
        )
    if public_hash_mismatch:
        mismatch_count = int(
            consistency.get("lineage_hash_mismatch_count", 0) or 0
        )
        add_points(
            "core_public_lineage_mismatch",
            "direct-context",
            15,
            (
                f"{mismatch_count} Bitcoin Core common-height hash "
                "comparison(s) disagree with the public best-chain lineage."
            ),
        )
    if public_source_disagreement_watch:
        add_points(
            "public_lineage_source_disagreement",
            "direct-context",
            15,
            (
                "Independent public sources report different best-chain "
                "hashes at the same height."
            ),
        )
    elif public_lineage_sources_disagree:
        reasons.append(
            "Independent public sources also disagree at the comparison height."
        )
    if core_lags_public:
        gap = int(
            consistency.get("maximum_public_minus_core_blocks", 0) or 0
        )
        fresh_count = int(
            consistency.get("public_height_fresh_node_count", 0) or 0
        )
        required_count = int(
            consistency.get("required_core_quorum", 1) or 1
        )
        add_points(
            "core_public_height_gap",
            "direct-context",
            15,
            (
                f"Only {fresh_count}/{required_count} required Bitcoin Core "
                f"nodes are near the public tip; the worst lag is {gap} blocks."
            ),
        )
    if core_age_watch:
        oldest_age = float(
            consistency.get("oldest_core_tip_age_minutes", 0.0) or 0.0
        )
        threshold = float(
            consistency.get(
                "absolute_stale_threshold_minutes", 180.0
            )
            or 180.0
        )
        add_points(
            "core_tip_absolute_age",
            "direct-context",
            15,
            (
                "The Bitcoin Core quorum has no recent tip and public "
                "height/lineage data does not confirm this is a network-wide "
                f"long block wait (oldest header {oldest_age:.0f} minutes; "
                f"threshold {threshold:.0f})."
            ),
        )
    if public_reference_lags:
        reasons.append(
            "The public reference tip materially trails the required Bitcoin Core node count."
        )
    if future_header_timestamp_count:
        reasons.append(
            (
                f"{future_header_timestamp_count} distinct miner-set tip "
                f"header timestamp(s), reported by "
                f"{future_header_timestamp_node_count} Core node(s), are "
                "more than two minutes ahead of observation time."
            )
        )

    timing = _value(observations.get("block_timing"))
    timing_level = str(timing.get("level", RiskLevel.UNKNOWN.value))
    if timing_level == RiskLevel.CRITICAL.value:
        add_points(
            "block_timing",
            "context",
            18,
            "Block timing is extremely unusual under the configured statistical model.",
        )
    elif timing_level == RiskLevel.WARNING.value:
        add_points(
            "block_timing",
            "context",
            12,
            "Block timing is statistically unusual.",
        )
    elif timing_level == RiskLevel.WATCH.value:
        add_points(
            "block_timing",
            "context",
            5,
            "Block timing is in the long-tail watch range.",
        )

    hashrate = _value(observations.get("network_hashrate"))
    monthly_change = hashrate.get("change_from_monthly_percent")
    if isinstance(monthly_change, (int, float)):
        if monthly_change <= -30:
            add_points(
                "network_hashrate",
                "context",
                15,
                "Estimated hashrate is at least 30% below its 30-day mean.",
            )
        elif monthly_change <= -20:
            add_points(
                "network_hashrate",
                "context",
                10,
                "Estimated hashrate is at least 20% below its 30-day mean.",
            )
        elif monthly_change <= -10:
            add_points(
                "network_hashrate",
                "context",
                5,
                "Estimated hashrate is at least 10% below its 30-day mean.",
            )

    pools = _value(observations.get("mining_pool_distribution"))
    largest_share = pools.get("largest_share_percent")
    largest_pool = str(pools.get("largest_pool") or "").strip().lower()
    attributable_pool = largest_pool not in {
        "",
        "unknown",
        "other",
        "unattributed",
        "unrecognized",
    }
    if attributable_pool and isinstance(largest_share, (int, float)):
        if largest_share >= 40:
            add_points(
                "mining_pool_concentration",
                "context",
                8,
                "Observed four-day block production is highly concentrated.",
            )
        elif largest_share >= 30:
            add_points(
                "mining_pool_concentration",
                "context",
                4,
                "Observed four-day block production is moderately concentrated.",
            )

    outages = _value(observations.get("blackout_news"))
    article_count = outages.get("article_count")
    if isinstance(article_count, int) and article_count > 0:
        add_points(
            "regional_outage_news",
            "context",
            min(5, article_count),
            "Recent outage-related news matched configured mining regions.",
        )

    context = _value(observations.get("research_context"))
    if context.get("near_halving") is True:
        add_points(
            "halving_proximity",
            "context",
            4,
            "The chain is within the configured halving context window.",
        )
    if context.get("in_research_time_window") is True:
        add_points(
            "paper_utc_window",
            "context",
            2,
            "The current UTC hour matches a paper-derived context window.",
        )

    direct_critical = reorg_depth >= 6 or (
        reorg_depth >= 3 and node_divergence
    )
    direct_warning = (
        reorg_depth >= 2
        or node_divergence
        or fork_depth >= 3
        or wallet_removed_at_risk > 0
    )
    direct_watch = (
        reorg_depth == 1
        or fork_depth in {1, 2}
        or bool(quality_event_kinds)
        or core_lags_public
        or public_hash_mismatch
        or public_source_disagreement_watch
        or core_age_watch
    )

    if direct_critical:
        level = RiskLevel.CRITICAL
    elif direct_warning:
        level = RiskLevel.WARNING
    elif direct_watch and score >= 15:
        level = RiskLevel.WATCH
    elif not chain_available or not quorum_met or single_node_only:
        level = RiskLevel.UNKNOWN
    elif score >= 50:
        level = RiskLevel.WARNING
    elif score >= 15:
        level = RiskLevel.WATCH
    else:
        level = RiskLevel.NORMAL

    if not chain_available:
        data_quality = "direct_chain_signals_unavailable"
        actions.append(
            "Configure independently hosted Bitcoin Core RPC nodes before treating a quiet result as normal."
        )
    elif not quorum_met:
        data_quality = "insufficient_node_quorum"
        actions.append(
            "Restore the configured Bitcoin Core node quorum and compare independently peered nodes."
        )
    elif (
        chain_observation is not None
        and chain_observation.status is not ObservationStatus.OK
    ) or quality_event_kinds:
        data_quality = "degraded_direct_chain_data"
        actions.append(
            "Inspect incomplete node comparisons and chain-quality events before treating the result as conclusive."
        )
    elif core_lags_public:
        data_quality = "core_nodes_lag_public_reference"
        actions.append(
            "Check Core peer connectivity, compare headers with another network path, and investigate possible eclipse or partition conditions."
        )
    elif public_hash_mismatch:
        data_quality = "core_public_lineage_mismatch"
        actions.append(
            "Compare the disputed height through another independent Core node and public source before trusting either tip."
        )
    elif public_lineage_sources_disagree:
        data_quality = "public_lineage_sources_disagree"
        actions.append(
            "Refetch the disputed height and compare a third independent source before trusting either public chain view."
        )
    elif core_age_watch:
        data_quality = "core_tip_age_not_publicly_reconciled"
        actions.append(
            "Restore or verify public height and common-height lineage data, then inspect Core peer connectivity before relying on this chain view."
        )
    elif future_header_timestamp_count:
        data_quality = "future_block_header_timestamp"
        actions.append(
            "Verify the miner-set block-header time through independent sources and treat age calculations cautiously; this alone does not establish a Core host-clock problem."
        )
    elif single_node_only:
        data_quality = "single_node_only"
        actions.append(
            "Add a second independently hosted Bitcoin Core node before treating a quiet result as normal."
        )
    elif public_reference_lags:
        data_quality = "public_reference_lagging"
        actions.append(
            "Verify the public reference and compare another independent Core node; rely on Core only after quorum agreement."
        )
    elif public_lineage_incomplete:
        data_quality = "core_public_lineage_unverified"
        actions.append(
            "Restore the public common-height hash source before treating the external lineage cross-check as complete."
        )
    elif any(
        observation.status.value not in {"ok", "partial"}
        for observation in observations.values()
        if observation.name in {
            "public_chain_tip",
            "recent_block_intervals",
            "network_hashrate",
        }
    ):
        data_quality = "degraded_context"
    else:
        data_quality = "sufficient"

    if level is RiskLevel.CRITICAL:
        summary = "Strong direct chain evidence requires immediate incident response."
        actions = [
            "Pause automatic deposit crediting and high-value settlement.",
            "Preserve node logs, competing block headers, and affected transaction data.",
            "Confirm the event across independent nodes and begin the incident runbook.",
            *actions,
        ]
        multiplier = None
        pause_settlement = True
    elif level is RiskLevel.WARNING:
        summary = "Direct or combined evidence warrants defensive controls."
        actions = [
            "Require manual review for high-value deposits.",
            "Temporarily increase confirmation requirements while validating chain continuity.",
            "Compare tips and chainwork across independent nodes.",
            *actions,
        ]
        multiplier = 2.0
        pause_settlement = False
    elif level is RiskLevel.WATCH:
        summary = "Anomalies are present, but they do not establish a chain attack."
        actions = [
            "Increase monitoring frequency and inspect direct chain signals.",
            "Avoid treating contextual market or news signals as proof.",
            *actions,
        ]
        multiplier = 1.5
        pause_settlement = False
    elif level is RiskLevel.NORMAL:
        summary = "No material anomaly is visible in the configured direct evidence."
        actions = ["Continue normal monitoring."]
        multiplier = 1.0
        pause_settlement = False
    else:
        summary = "Risk is unknown because decisive direct-chain evidence is incomplete."
        if not actions:
            actions = ["Restore direct-chain data sources before making an automated decision."]
        multiplier = None
        pause_settlement = False

    if not reasons:
        reasons.append("No rule threshold was crossed by available observations.")

    return RiskAssessment(
        level=level,
        evidence_score=score,
        summary=summary,
        reasons=tuple(reasons),
        actions=tuple(dict.fromkeys(actions)),
        data_quality=data_quality,
        confirmation_multiplier=multiplier,
        pause_settlement=pause_settlement,
        score_components=tuple(components),
    )
