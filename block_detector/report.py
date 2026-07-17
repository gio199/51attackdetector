from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .ai import AIRecommendation


_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _observation(
    observations: Mapping[str, Any], name: str
) -> Mapping[str, Any]:
    return _mapping(observations.get(name))


def _value(observations: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return _mapping(_observation(observations, name).get("value"))


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value: object, *, digits: int = 2) -> str:
    number = _float(value)
    if number is None:
        return "N/A"
    absolute = abs(number)
    for threshold, suffix in (
        (1_000_000_000_000, "T"),
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ):
        if absolute >= threshold:
            return f"{number / threshold:,.{digits}f}{suffix}"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.{digits}f}"


def _integer(value: object) -> str:
    number = _float(value)
    return f"{int(number):,}" if number is not None else "N/A"


def _scientific(value: object) -> str:
    number = _float(value)
    return f"{number:.6g}" if number is not None else "N/A"


def _usd(value: object) -> str:
    return f"${_number(value)}" if _float(value) is not None else "N/A"


def _percent(
    value: object,
    *,
    fraction: bool = False,
    digits: int = 2,
) -> str:
    number = _float(value)
    if number is None:
        return "N/A"
    if fraction:
        number *= 100
    return f"{number:+.{digits}f}%"


def _probability(value: object) -> str:
    number = _float(value)
    if number is None:
        return "N/A"
    if number == 0:
        return "0"
    if number < 0.001:
        return f"{number:.2e}"
    return f"{number:.4f}"


def _yes_no(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "N/A"


def _text(value: object, *, limit: int = 180) -> str:
    if value is None:
        return "N/A"
    raw = _ANSI_ESCAPE.sub("", str(value))
    characters: list[str] = []
    for character in raw:
        if character.isspace():
            characters.append(" ")
        elif unicodedata.category(character) in {"Cc", "Cf"}:
            continue
        else:
            characters.append(character)
    result = " ".join("".join(characters).split())
    if len(result) <= limit:
        return result
    return result[: max(0, limit - 3)] + "..."


def _section(lines: list[str], title: str) -> None:
    lines.extend(("", title, "-" * len(title)))


def _status(observation: Mapping[str, Any]) -> str:
    return str(observation.get("status", "unavailable")).upper()


def _ratio(numerator: object, denominator: object) -> str:
    top = _float(numerator)
    bottom = _float(denominator)
    if top is None or bottom is None or bottom == 0:
        return "N/A"
    return f"{top / bottom:.3f}"


def _unix_age_minutes(value: object, generated_at: object) -> float | None:
    timestamp = _float(value)
    if timestamp is None or generated_at is None:
        return None
    try:
        current = datetime.fromisoformat(
            str(generated_at).replace("Z", "+00:00")
        )
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        block_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return max(
            0.0,
            (
                current.astimezone(timezone.utc) - block_time
            ).total_seconds()
            / 60.0,
        )
    except (OSError, OverflowError, ValueError):
        return None


def _list_items(value: object) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def render_report(
    snapshot: Mapping[str, Any],
    ai_recommendation: AIRecommendation | None = None,
) -> str:
    assessment = _mapping(snapshot.get("assessment"))
    observations = _mapping(snapshot.get("observations"))
    level = str(assessment.get("level", "unknown")).upper()
    score = assessment.get("evidence_score", "N/A")

    lines = [
        "=" * 72,
        "BITCOIN 51% ATTACK RISK MONITOR",
        "=" * 72,
        f"Generated: {snapshot.get('generated_at', 'N/A')}",
        (
            f"LEVEL: {level}   EVIDENCE SCORE: {score}/100   "
            f"DATA: {assessment.get('data_quality', 'unknown')}"
        ),
        _text(assessment.get("summary"), limit=500),
        "The evidence score is a rule index, not an attack probability.",
    ]

    _section(lines, "DIRECT CHAIN EVIDENCE")
    chain_observation = _observation(observations, "chain_signals")
    chain = _mapping(chain_observation.get("value"))
    if not chain:
        lines.append(
            f"Bitcoin Core RPC: {_status(chain_observation)} - "
            f"{_text(chain_observation.get('error'))}"
        )
    else:
        healthy = int(chain.get("healthy_node_count", 0) or 0)
        configured = int(chain.get("configured_node_count", 0) or 0)
        required = int(chain.get("minimum_healthy_nodes", 0) or 0)
        lines.append(
            f"RPC nodes: {healthy}/{configured} healthy; quorum "
            f"{_yes_no(chain.get('quorum_met'))} (required {required})"
        )
        comparison_count = int(
            chain.get(
                "common_height_comparison_count",
                len(_mapping(chain.get("hashes_at_common_height"))),
            )
            or 0
        )
        if healthy == 1:
            lines.append(
                "Cross-node confirmation: unavailable (single healthy source)"
            )
        else:
            if comparison_count < required:
                agreement = "indeterminate"
            else:
                agreement = (
                    "no"
                    if chain.get("node_divergence") is True
                    else "yes among compared nodes"
                )
            lines.append(
                f"Common height: {_integer(chain.get('common_height'))}; "
                f"node agreement: {agreement} "
                f"({comparison_count}/{healthy} compared); "
                f"height spread: {_integer(chain.get('height_spread'))}"
            )
        lines.append(
            f"Maximum observed reorg: "
            f"{_integer(chain.get('max_reorg_depth'))} blocks; "
            f"valid competing branch: "
            f"{_integer(chain.get('max_valid_fork_branch_length'))} blocks"
        )
        if chain.get("wallet_monitoring_enabled") is True:
            lines.append(
                "Wallet removals still at risk: "
                f"{_integer(chain.get('wallet_removed_at_risk_count'))}"
            )
        else:
            lines.append("Wallet reorg monitoring: disabled")
        quality_events = _list_items(chain.get("quality_events"))
        quality_kinds = _list_items(chain.get("quality_event_kinds"))
        if quality_events or quality_kinds:
            kinds = sorted(
                {
                    str(item.get("kind"))
                    for item in quality_events
                    if isinstance(item, Mapping) and item.get("kind")
                }
                | {str(item) for item in quality_kinds}
            )
            lines.append(
                f"Chain quality events: {len(quality_events) or len(kinds)} "
                f"({', '.join(kinds) or 'unspecified'})"
            )
            for event in quality_events:
                event_value = _mapping(event)
                lines.append(
                    f"  {_text(event_value.get('node_id'), limit=80)}: "
                    f"{_text(event_value.get('kind'), limit=100)}"
                )
        skipped_state_nodes = _list_items(
            chain.get("state_update_skipped_nodes")
        )
        if skipped_state_nodes:
            lines.append(
                "Trusted baseline preserved for: "
                + ", ".join(
                    _text(node_id, limit=80)
                    for node_id in skipped_state_nodes
                )
            )
        for node in _list_items(chain.get("nodes")):
            node_value = _mapping(node)
            progress = _float(node_value.get("verification_progress"))
            tip_age = _unix_age_minutes(
                node_value.get("block_time"),
                snapshot.get("generated_at"),
            )
            progress_text = (
                f"{progress * 100:.3f}%" if progress is not None else "N/A"
            )
            lines.append(
                f"  {_text(node_value.get('node_id'), limit=80)}: "
                f"height {_integer(node_value.get('height'))}, "
                f"tip age {_number(tip_age)} min, "
                f"verified {progress_text}, "
                f"pruned {_yes_no(node_value.get('pruned'))}"
            )

    consistency_observation = _observation(
        observations, "chain_public_consistency"
    )
    consistency = _mapping(consistency_observation.get("value"))
    if consistency:
        required_core = _integer(
            consistency.get("required_core_quorum")
        )
        if consistency.get("public_reference_available") is True:
            worst_gap = _float(
                consistency.get("maximum_public_minus_core_blocks")
            )
            worst_gap_text = (
                f"{worst_gap:+.0f}" if worst_gap is not None else "N/A"
            )
            lines.append(
                f"Core/public height cross-check "
                f"[{_status(consistency_observation)}]: "
                f"public {_integer(consistency.get('public_height'))}, "
                f"Core {_integer(consistency.get('lowest_core_height'))}-"
                f"{_integer(consistency.get('highest_core_height'))}; "
                f"fresh {_integer(consistency.get('public_height_fresh_node_count'))}/"
                f"{required_core} required, worst public-Core gap "
                f"{worst_gap_text} blocks "
                f"(threshold {_integer(consistency.get('lag_threshold_blocks'))})"
            )
        else:
            lines.append(
                f"Core/public height cross-check "
                f"[{_status(consistency_observation)}]: public reference "
                f"unavailable; Core "
                f"{_integer(consistency.get('lowest_core_height'))}-"
                f"{_integer(consistency.get('highest_core_height'))}"
            )

        lines.append(
            "Common-height Core/public lineage checks: height "
            f"{_integer(consistency.get('lineage_height'))}, "
            f"{_integer(consistency.get('lineage_hash_comparison_count'))}/"
            f"{required_core} required compared, "
            f"{_integer(consistency.get('lineage_hash_match_count'))} "
            "matched, "
            f"{_integer(consistency.get('lineage_hash_mismatch_count'))} "
            f"mismatched; source "
            f"{_text(consistency.get('lineage_source_kind'), limit=50)} "
            f"{_text(consistency.get('lineage_source_status'), limit=30)}"
        )

        if consistency.get("core_tip_age_comparison_count"):
            lines.append(
                "Absolute Core tip-header age check: "
                f"{_integer(consistency.get('absolute_fresh_core_node_count'))}/"
                f"{required_core} required nodes newer than "
                f"{_number(consistency.get('absolute_stale_threshold_minutes'))} "
                "min; oldest "
                f"{_number(consistency.get('oldest_core_tip_age_minutes'))} min"
            )

        if (
            consistency.get("core_quorum_materially_behind_public")
            is True
        ):
            lines.append(
                "  Warning: the required Core fresh-node quorum is not near "
                "the independent public reference."
            )
        if consistency.get("public_core_lineage_mismatch") is True:
            lines.append(
                "  Warning: Core and the public best chain disagree at their "
                "common lineage height."
            )
        elif (
            consistency.get(
                "public_core_lineage_comparison_incomplete"
            )
            is True
        ):
            lines.append(
                "  Gap: public lineage could not be compared with the "
                "required Core quorum."
            )
        if consistency.get("public_lineage_sources_disagree") is True:
            lines.append(
                "  Warning: the independent public sources disagree at the "
                "same height; the fresh public tip was used for comparison."
            )
        if consistency.get("core_staleness_unresolved") is True:
            lines.append(
                "  Warning: old Core tip headers are not reconciled by a "
                "matching public height and lineage view."
            )
        elif (
            consistency.get("public_confirms_network_wide_old_tip")
            is True
        ):
            lines.append(
                "  Note: the public height and lineage confirm the same old "
                "tip; the statistical block-timing rule handles that wait."
            )
        if (
            consistency.get("public_reference_materially_behind_core") is True
        ):
            lines.append(
                "  Warning: the public reference materially trails the "
                "required Core node count."
            )
        future_header_count = int(
            consistency.get("future_header_timestamp_count", 0) or 0
        )
        if future_header_count:
            lines.append(
                f"  Caution: {future_header_count} distinct miner-set tip "
                "header timestamp(s) are more than two minutes in the future."
            )
    else:
        lines.append(
            f"Core/public cross-check: {_status(consistency_observation)} - "
            f"{_text(consistency_observation.get('error'))}"
        )

    _section(lines, "BLOCK PRODUCTION")
    tip_observation = _observation(observations, "public_chain_tip")
    tip = _mapping(tip_observation.get("value"))
    if tip:
        header_time_note = (
            " [FUTURE MINER-SET HEADER TIME]"
            if tip.get("future_header_timestamp") is True
            else ""
        )
        lines.append(
            f"Public tip: height {_integer(tip.get('height'))}, "
            f"age {_number(tip.get('age_minutes'))} min, "
            f"time {tip.get('block_time', 'N/A')}{header_time_note}"
        )
    else:
        lines.append(
            f"Public tip: {_status(tip_observation)} - "
            f"{_text(tip_observation.get('error'))}"
        )

    timing_observation = _observation(observations, "block_timing")
    timing = _mapping(timing_observation.get("value"))
    if timing:
        lines.append(
            f"Timing model: {str(timing.get('level', 'unknown')).upper()}; "
            f"current-wait tail p={_probability(timing.get('age_tail_probability'))}; "
            f"recent-mean tail p="
            f"{_probability(timing.get('recent_average_tail_probability'))}"
        )
        lines.append(f"  {_text(timing.get('reason'), limit=500)}")
    else:
        lines.append(
            f"Timing model: {_status(timing_observation)} - "
            f"{_text(timing_observation.get('error'))}"
        )

    recent_observation = _observation(observations, "recent_block_intervals")
    recent = _mapping(recent_observation.get("value"))
    if recent:
        intervals = [
            number
            for item in _list_items(recent.get("intervals_minutes"))
            if (number := _float(item)) is not None
        ]
        latest_ten = intervals[:10]
        last_ten_average = (
            sum(latest_ten) / len(latest_ten) if latest_ten else None
        )
        lines.append(
            f"Recent intervals: {_number(recent.get('average_minutes'))} min avg, "
            f"{_number(recent.get('median_minutes'))} median, "
            f"{_number(recent.get('p95_minutes'))} p95 "
            f"({len(intervals)} intervals)"
        )
        lines.append(
            "Original-paper last-10 timing proxy: "
            f"{_number(last_ten_average)} min across {len(latest_ten)} intervals"
        )
    else:
        lines.append(
            f"Recent intervals: {_status(recent_observation)} - "
            f"{_text(recent_observation.get('error'))}"
        )

    _section(lines, "NETWORK AND MINING CONTEXT")
    hashrate_observation = _observation(observations, "network_hashrate")
    hashrate = _mapping(hashrate_observation.get("value"))
    if hashrate:
        unit = _text(
            hashrate_observation.get("unit") or "provider units",
            limit=60,
        )
        lines.append(
            f"Network hashrate [{_status(hashrate_observation)}]: "
            f"{_number(hashrate.get('current'))} {unit}; "
            f"7d change {_percent(hashrate.get('change_from_weekly_percent'))}; "
            f"30d change {_percent(hashrate.get('change_from_monthly_percent'))}"
        )
        lines.append(
            "Original-paper average/current hashrate ratio: "
            f"{_ratio(hashrate.get('monthly_average'), hashrate.get('current'))}; "
            f"estimate age {_number(hashrate.get('age_hours'))} h"
        )
    else:
        lines.append(
            f"Network hashrate: {_status(hashrate_observation)} - "
            f"{_text(hashrate_observation.get('error'))}"
        )

    pools_observation = _observation(observations, "mining_pool_distribution")
    pools = _mapping(pools_observation.get("value"))
    if pools:
        if pools.get("largest_pool") is not None:
            lines.append(
                f"Largest attributable pool: "
                f"{_text(pools.get('largest_pool'), limit=80)} "
                f"at {_number(pools.get('largest_share_percent'))}% of "
                f"{_integer(pools.get('observed_blocks'))} blocks/4d; "
                f"attributed HHI lower bound "
                f"{_number(pools.get('herfindahl_index'), digits=3)}"
            )
        else:
            lines.append("Largest attributable pool: unavailable")
        lines.append(
            f"Unattributed block share: "
            f"{_number(pools.get('unattributed_share_percent'))}% "
            f"({_integer(pools.get('unattributed_blocks'))} blocks)"
        )
    else:
        lines.append(
            f"Mining pools: {_status(pools_observation)} - "
            f"{_text(pools_observation.get('error'))}"
        )

    nicehash_observation = _observation(
        observations, "nicehash_sha256_context"
    )
    nicehash = _mapping(nicehash_observation.get("value"))
    algorithms = _list_items(nicehash.get("algorithms"))
    if algorithms:
        lines.append(
            "NiceHash SHA256 delivered speed (context, not spare attack capacity):"
        )
        for algorithm in algorithms:
            item = _mapping(algorithm)
            name = item.get("name") or f"algorithm {item.get('algorithm_id', 'N/A')}"
            lines.append(
                f"  {_text(name, limit=60)}: current raw "
                f"{_scientific(item.get('active_speed_vendor_units'))}, "
                f"24h raw "
                f"{_scientific(item.get('24h_active_speed_vendor_units'))}; "
                f"24h/current ratio "
                f"{_ratio(item.get('24h_active_speed_vendor_units'), item.get('active_speed_vendor_units'))}; "
                f"provider unit label "
                f"{_text(item.get('speed_unit') or 'N/A', limit=30)}, "
                f"multiplier {_scientific(item.get('speed_multiplier'))}"
            )
    else:
        lines.append(
            f"NiceHash SHA256 context: {_status(nicehash_observation)} - "
            f"{_text(nicehash_observation.get('error'))}"
        )

    _section(lines, "PAPER, MARKET, AND EXTERNAL CONTEXT")
    market_observation = _observation(observations, "market_context")
    market = _mapping(market_observation.get("value"))
    if market:
        lines.append(
            f"BTC price: {_usd(market.get('btc_price_usd'))} "
            f"[{_status(market_observation)}]"
        )
        bitfinex = _mapping(market.get("bitfinex_margin_positions"))
        if bitfinex:
            lines.append(
                f"Bitfinex margin positions: shorts "
                f"{_usd(bitfinex.get('shorts_usd'))}, longs "
                f"{_usd(bitfinex.get('longs_usd'))}, short/long ratio "
                f"{_ratio(bitfinex.get('shorts_usd'), bitfinex.get('longs_usd'))}"
            )
        for venue, label in (
            ("bybit_derivatives", "Bybit"),
            ("okx_derivatives", "OKX"),
        ):
            derivatives = _mapping(market.get(venue))
            if derivatives:
                lines.append(
                    f"{label}: open interest "
                    f"{_usd(derivatives.get('open_interest_usd'))}, funding "
                    f"{_percent(derivatives.get('funding_rate'), fraction=True, digits=4)}"
                )
    else:
        lines.append(
            f"Market context: {_status(market_observation)} - "
            f"{_text(market_observation.get('error'))}"
        )

    bch_observation = _observation(observations, "bitcoin_cash_context")
    bch = _mapping(bch_observation.get("value"))
    if bch:
        lines.append(
            f"Bitcoin Cash: price {_usd(bch.get('market_price_usd'))}, "
            f"24h hashrate {_number(bch.get('hashrate_24h'))}, "
            f"average fee {_number(bch.get('average_transaction_fee_24h'))} "
            "(provider units)"
        )
        lines.append(
            "Paper BCH/BTC fee expression: incomplete "
            "(BTC fee input and normalized fee units unavailable)"
        )
    else:
        lines.append(
            f"Bitcoin Cash context: {_status(bch_observation)} - "
            f"{_text(bch_observation.get('error'))}"
        )

    research_observation = _observation(observations, "research_context")
    research = _mapping(research_observation.get("value"))
    if research:
        lines.append(
            f"UTC paper window: hour {_integer(research.get('utc_hour'))}, "
            f"active {_yes_no(research.get('in_research_time_window'))}"
        )
        lines.append(
            f"Halving context: near {_yes_no(research.get('near_halving'))}; "
            f"{_integer(research.get('blocks_until_halving'))} blocks until, "
            f"{_integer(research.get('blocks_since_halving'))} since"
        )

    news_observation = _observation(observations, "blackout_news")
    news = _mapping(news_observation.get("value"))
    if news:
        lines.append(
            f"Regional outage/mining news: "
            f"{_integer(news.get('article_count'))} keyword-and-region matches over "
            f"{_integer(news.get('days_back'))} days via "
            f"{news_observation.get('source', 'N/A')}"
        )
        for article in _list_items(news.get("articles"))[:3]:
            item = _mapping(article)
            locations = item.get("locations")
            location_text = (
                ", ".join(str(value) for value in locations)
                if isinstance(locations, list) and locations
                else "region not identified"
            )
            lines.append(
                f"  {_text(item.get('title'), limit=120)} [{location_text}]"
            )
    else:
        lines.append(
            f"Regional news: {_status(news_observation)} - "
            f"{_text(news_observation.get('error'))}"
        )
    lines.append(
        "Unresolved paper inputs: normalized rentable capacity and short volume. "
        "The raw multiplicative paper formula is therefore not calculated."
    )

    _section(lines, "WHY THIS SCORE")
    components = _list_items(assessment.get("score_components"))
    if components:
        for component in components:
            item = _mapping(component)
            lines.append(
                f"+{int(item.get('points', 0) or 0):>2} "
                f"[{item.get('category', 'context')}] "
                f"{item.get('signal', 'signal')} ({item.get('rule', 'rule')}): "
                f"{_text(item.get('detail'), limit=500)}"
            )
    else:
        for reason in _list_items(assessment.get("reasons")):
            lines.append(f"- {_text(reason, limit=500)}")

    _section(lines, "DETERMINISTIC RECOMMENDATIONS")
    actions = _list_items(assessment.get("actions"))
    if actions:
        for action in actions:
            lines.append(f"- {_text(action, limit=500)}")
    else:
        lines.append("- No deterministic action was supplied.")
    multiplier = assessment.get("confirmation_multiplier")
    lines.append(
        f"Confirmation multiplier: "
        f"{multiplier if multiplier is not None else 'N/A'}; "
        f"pause settlement: {_yes_no(assessment.get('pause_settlement'))}"
    )

    _section(lines, "AI ADVISORY")
    recommendation = ai_recommendation or AIRecommendation.unavailable(
        "AI advisory was not requested"
    )
    if recommendation.status == "ok":
        lines.append(
            f"OpenAI model: "
            f"{_text(recommendation.model or 'configured model', limit=100)}"
        )
        lines.append(_text(recommendation.summary, limit=1200))
        for check in recommendation.checks:
            lines.append(f"- {_text(check, limit=500)}")
        for caveat in recommendation.caveats:
            lines.append(f"  Caveat: {_text(caveat, limit=500)}")
    else:
        lines.append(
            f"{recommendation.status.upper()}: {_text(recommendation.error, limit=500)}"
        )
        lines.append(
            "The deterministic recommendations above remain authoritative."
        )

    _section(lines, "DATA SOURCE GAPS")
    gaps: list[str] = []
    for name, raw_observation in observations.items():
        observation = _mapping(raw_observation)
        status = str(observation.get("status", "unavailable"))
        error = observation.get("error")
        metadata = _mapping(observation.get("metadata"))
        if status != "ok":
            detail = f"{name}: {status}"
            if error:
                detail += f" - {_text(error)}"
            gaps.append(detail)
        primary_error = metadata.get("primary_error")
        if primary_error:
            gaps.append(
                f"{name}: primary source failed; fallback used - "
                f"{_text(primary_error)}"
            )
        for label in ("errors", "excluded"):
            entries = _mapping(metadata.get(label))
            for source, source_error in entries.items():
                gaps.append(
                    f"{name}/{_text(source, limit=80)}: {label[:-1]} - "
                    f"{_text(source_error)}"
                )
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("- No collector gaps reported.")

    lines.extend(("", "=" * 72))
    return "\n".join(lines)
