from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .http import JsonHttpClient
from .models import Observation, ObservationStatus, utc_now
from .statistics import intervals_from_blocks


LATEST_BLOCK_URL = "https://blockchain.info/latestblock"
BLOCKCHAIR_BLOCKS_URL = "https://api.blockchair.com/bitcoin/blocks"
MEMPOOL_BLOCKS_URL = "https://mempool.space/api/blocks"
MEMPOOL_BLOCKS_AT_HEIGHT_URL = "https://mempool.space/api/blocks/{height}"
HASHRATE_URL = "https://api.blockchain.info/charts/hash-rate"
POOL_DISTRIBUTION_URL = "https://api.blockchain.info/pools"
NEWS_API_URL = "https://newsapi.org/v2/everything"
GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
BCH_STATS_URL = "https://api.blockchair.com/bitcoin-cash/stats"
NICEHASH_CURRENT_URL = (
    "https://api2.nicehash.com/main/api/v2/public/stats/global/current/"
)
NICEHASH_24H_URL = "https://api2.nicehash.com/main/api/v2/public/stats/global/24h/"
NICEHASH_BUY_INFO_URL = "https://api2.nicehash.com/main/api/v2/public/buy/info/"


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} response must be an object")
    return value


class PublicChainCollector:
    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient()

    def collect(self, *, now: datetime | None = None) -> Observation:
        observed_at = now or utc_now()
        try:
            data = _require_mapping(
                self.http.get(LATEST_BLOCK_URL), "latest block"
            )
            block_time = datetime.fromtimestamp(int(data["time"]), tz=timezone.utc)
            age_minutes = max(
                0.0, (observed_at - block_time).total_seconds() / 60.0
            )
            return Observation.ok(
                "public_chain_tip",
                "blockchain.info/latestblock",
                {
                    "height": int(data["height"]),
                    "hash": str(data["hash"]),
                    "block_time": block_time.isoformat(),
                    "age_minutes": age_minutes,
                    "future_header_timestamp": block_time > observed_at,
                },
                observed_at=observed_at,
            )
        except Exception as exc:
            return Observation.failed(
                "public_chain_tip",
                "blockchain.info/latestblock",
                str(exc),
                observed_at=observed_at,
            )


class PublicBlockHashCollector:
    """Fetch the public best-chain hash at a specific comparison height."""

    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient()

    def collect(
        self,
        *,
        height: int,
        now: datetime | None = None,
    ) -> Observation:
        observed_at = now or utc_now()
        target_height = int(height)
        if target_height < 0:
            return Observation.failed(
                "public_common_height_hash",
                "mempool.space/api/blocks",
                "Comparison height must be non-negative",
                observed_at=observed_at,
            )
        try:
            data = self.http.get(
                MEMPOOL_BLOCKS_AT_HEIGHT_URL.format(
                    height=target_height
                )
            )
            if not isinstance(data, list):
                raise ValueError("public blocks response must be a list")
            block: Mapping[str, Any] | None = None
            for item in data:
                if not isinstance(item, Mapping):
                    continue
                try:
                    item_height = int(item.get("height", -1))
                except (TypeError, ValueError):
                    continue
                if item_height == target_height:
                    block = item
                    break
            if not isinstance(block, Mapping):
                raise ValueError(
                    "public blocks response omitted the requested height"
                )
            block_hash = str(block["id"]).strip()
            if not block_hash:
                raise ValueError("public block hash is empty")
            return Observation.ok(
                "public_common_height_hash",
                "mempool.space/api/blocks",
                {
                    "height": target_height,
                    "hash": block_hash,
                },
                observed_at=observed_at,
                metadata={
                    "description": (
                        "Public best-chain hash used only for common-height "
                        "lineage comparison with Bitcoin Core."
                    )
                },
            )
        except Exception as exc:
            return Observation.failed(
                "public_common_height_hash",
                "mempool.space/api/blocks",
                str(exc),
                observed_at=observed_at,
            )


class RecentBlocksCollector:
    def __init__(
        self, http: JsonHttpClient | None = None, *, limit: int = 24
    ) -> None:
        self.http = http or JsonHttpClient()
        self.limit = max(2, min(int(limit), 100))

    def collect(self, *, now: datetime | None = None) -> Observation:
        observed_at = now or utc_now()
        try:
            source = "blockchair.com/bitcoin/blocks"
            primary_error: str | None = None
            blocks: list[tuple[int, datetime]] = []
            try:
                response = _require_mapping(
                    self.http.get(
                        BLOCKCHAIR_BLOCKS_URL,
                        params={"limit": self.limit, "s": "id(desc)"},
                    ),
                    "recent blocks",
                )
                data = response.get("data")
                if not isinstance(data, list) or len(data) < 2:
                    raise ValueError(
                        "recent blocks response contains fewer than two blocks"
                    )
                for item in data:
                    mapping = _require_mapping(item, "block")
                    timestamp = datetime.strptime(
                        str(mapping["time"]), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                    if timestamp <= observed_at + timedelta(minutes=2):
                        blocks.append((int(mapping["id"]), timestamp))
            except Exception as exc:
                primary_error = str(exc)
                source = "mempool.space/api/blocks"
                fallback = self.http.get(MEMPOOL_BLOCKS_URL)
                if not isinstance(fallback, list) or len(fallback) < 2:
                    raise ValueError(
                        "mempool.space fallback contains fewer than two blocks"
                    ) from exc
                for item in fallback[: self.limit]:
                    mapping = _require_mapping(item, "mempool.space block")
                    timestamp = datetime.fromtimestamp(
                        int(mapping["timestamp"]), tz=timezone.utc
                    )
                    if timestamp <= observed_at + timedelta(minutes=2):
                        blocks.append((int(mapping["height"]), timestamp))

            intervals = intervals_from_blocks(blocks)
            if not intervals:
                raise ValueError("no consecutive, valid block intervals were returned")
            sorted_intervals = sorted(intervals)
            p95_index = min(
                len(sorted_intervals) - 1,
                max(0, round(0.95 * (len(sorted_intervals) - 1))),
            )
            return Observation.ok(
                "recent_block_intervals",
                source,
                {
                    "block_count": len(blocks),
                    "interval_count": len(intervals),
                    "intervals_minutes": intervals,
                    "average_minutes": statistics.fmean(intervals),
                    "median_minutes": statistics.median(intervals),
                    "p95_minutes": sorted_intervals[p95_index],
                    "highest_height": max(height for height, _ in blocks),
                },
                unit="minutes",
                observed_at=observed_at,
                metadata={
                    "primary_error": primary_error,
                    "description": (
                        "Inter-block intervals, not transaction confirmation time."
                    )
                },
            )
        except Exception as exc:
            return Observation.failed(
                "recent_block_intervals",
                "blockchair.com/bitcoin/blocks",
                str(exc),
                observed_at=observed_at,
            )


def calculate_hashrate_metrics(
    values: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
) -> dict[str, Any]:
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)

    parsed: list[tuple[datetime, float]] = []
    for item in values:
        timestamp = datetime.fromtimestamp(int(item["x"]), tz=timezone.utc)
        value = float(item["y"])
        if timestamp <= as_of + timedelta(minutes=2):
            parsed.append((timestamp, value))
    if not parsed:
        raise ValueError("hashrate response has no usable values")
    parsed.sort(key=lambda item: item[0])

    def window(days: int) -> list[float]:
        boundary = as_of - timedelta(days=days)
        return [value for timestamp, value in parsed if boundary <= timestamp <= as_of]

    weekly_values = window(7)
    monthly_values = window(30)
    if not weekly_values or not monthly_values:
        raise ValueError("hashrate response does not cover the required time windows")

    current_timestamp, current_hashrate = parsed[-1]
    weekly_average = statistics.fmean(weekly_values)
    monthly_average = statistics.fmean(monthly_values)
    return {
        "current": current_hashrate,
        "current_timestamp": current_timestamp.isoformat(),
        "weekly_average": weekly_average,
        "monthly_average": monthly_average,
        "change_from_weekly_percent": (
            ((current_hashrate - weekly_average) / weekly_average) * 100
            if weekly_average
            else None
        ),
        "change_from_monthly_percent": (
            ((current_hashrate - monthly_average) / monthly_average) * 100
            if monthly_average
            else None
        ),
        "weekly_sample_count": len(weekly_values),
        "monthly_sample_count": len(monthly_values),
        "age_hours": (as_of - current_timestamp).total_seconds() / 3600.0,
    }


class HashrateCollector:
    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient()

    def collect(self, *, now: datetime | None = None) -> Observation:
        observed_at = now or utc_now()
        try:
            data = _require_mapping(
                self.http.get(
                    HASHRATE_URL,
                    params={
                        "format": "json",
                        "timespan": "35days",
                        "rollingAverage": "8hours",
                        "sampled": "false",
                    },
                ),
                "hashrate",
            )
            values = data.get("values")
            if not isinstance(values, list):
                raise ValueError("hashrate response has no values list")
            metrics = calculate_hashrate_metrics(values, as_of=observed_at)
            status = (
                ObservationStatus.STALE
                if float(metrics["age_hours"]) > 48
                else ObservationStatus.OK
            )
            return Observation(
                name="network_hashrate",
                source="blockchain.com/charts/hash-rate",
                status=status,
                observed_at=observed_at,
                value=metrics,
                unit=str(data.get("unit") or "provider unit"),
                error=(
                    "Latest hashrate estimate is more than 48 hours old"
                    if status is ObservationStatus.STALE
                    else None
                ),
                metadata={
                    "period": data.get("period"),
                    "rolling_average": "8hours",
                },
            )
        except Exception as exc:
            return Observation.failed(
                "network_hashrate",
                "blockchain.com/charts/hash-rate",
                str(exc),
                observed_at=observed_at,
            )


class PoolDistributionCollector:
    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient()

    def collect(self, *, now: datetime | None = None) -> Observation:
        observed_at = now or utc_now()
        try:
            raw = _require_mapping(
                self.http.get(POOL_DISTRIBUTION_URL, params={"timespan": "4days"}),
                "pool distribution",
            )
            counts = {
                str(pool): int(blocks)
                for pool, blocks in raw.items()
                if int(blocks) >= 0
            }
            total = sum(counts.values())
            if total <= 0:
                raise ValueError("pool distribution contains no mined blocks")
            unattributed_names = {
                "unknown",
                "other",
                "unattributed",
                "unrecognized",
            }
            attributed = {
                pool: count
                for pool, count in counts.items()
                if pool.strip().lower() not in unattributed_names
            }
            unattributed_count = total - sum(attributed.values())
            if attributed:
                largest_pool, largest_count = max(
                    attributed.items(), key=lambda item: item[1]
                )
                largest_share_percent: float | None = (
                    largest_count / total * 100
                )
            else:
                largest_pool = None
                largest_share_percent = None
            shares = {pool: count / total for pool, count in counts.items()}
            attributed_shares = {
                pool: count / total for pool, count in attributed.items()
            }
            return Observation.ok(
                "mining_pool_distribution",
                "blockchain.com/pools",
                {
                    "timespan": "4days",
                    "observed_blocks": total,
                    "largest_pool": largest_pool,
                    "largest_share_percent": largest_share_percent,
                    "unattributed_blocks": unattributed_count,
                    "unattributed_share_percent": (
                        unattributed_count / total * 100
                    ),
                    "herfindahl_index": sum(
                        share * share for share in attributed_shares.values()
                    ),
                    "pool_count": len(attributed),
                    "reported_category_count": len(counts),
                    "shares_percent": {
                        pool: share * 100 for pool, share in shares.items()
                    },
                },
                observed_at=observed_at,
                partial=unattributed_count > 0,
                metadata={
                    "description": (
                        "Observed block share is a lagging proxy, not direct pool "
                        "hashrate. Unattributed blocks are excluded from largest-pool "
                        "scoring; the HHI is therefore a lower bound."
                    )
                },
            )
        except Exception as exc:
            return Observation.failed(
                "mining_pool_distribution",
                "blockchain.com/pools",
                str(exc),
                observed_at=observed_at,
            )


class BCHContextCollector:
    def __init__(
        self,
        http: JsonHttpClient | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self.http = http or JsonHttpClient()
        self.api_key = api_key

    def collect(self, *, now: datetime | None = None) -> Observation:
        observed_at = now or utc_now()
        try:
            params = {"key": self.api_key} if self.api_key else None
            response = _require_mapping(
                self.http.get(BCH_STATS_URL, params=params),
                "Bitcoin Cash stats",
            )
            data = _require_mapping(response.get("data"), "Bitcoin Cash data")
            fields = (
                "best_block_height",
                "best_block_hash",
                "best_block_time",
                "hashrate_24h",
                "transactions_24h",
                "blocks_24h",
                "average_transaction_fee_24h",
                "median_transaction_fee_24h",
                "market_price_usd",
                "mempool_transactions",
                "mempool_size",
                "mempool_total_fee_usd",
            )
            value = {field: data.get(field) for field in fields}
            if value["best_block_height"] is None:
                raise ValueError("Bitcoin Cash stats have no best block height")
            return Observation.ok(
                "bitcoin_cash_context",
                "blockchair.com/bitcoin-cash/stats",
                value,
                observed_at=observed_at,
                metadata={
                    "description": (
                        "Same-algorithm-chain context; volume and fees are not direct attack evidence."
                    )
                },
            )
        except Exception as exc:
            return Observation.failed(
                "bitcoin_cash_context",
                "blockchair.com/bitcoin-cash/stats",
                str(exc),
                observed_at=observed_at,
            )


def _nicehash_algorithms(payload: Any) -> dict[int, dict[str, Any]]:
    mapping = _require_mapping(payload, "NiceHash stats")
    raw_algorithms = mapping.get("algos")
    if not isinstance(raw_algorithms, list):
        raise ValueError("NiceHash stats have no algos list")
    result: dict[int, dict[str, Any]] = {}
    for item in raw_algorithms:
        if not isinstance(item, Mapping) or item.get("a") is None:
            continue
        algorithm_id = int(item["a"])
        result[algorithm_id] = {
            "algorithm_id": algorithm_id,
            "paying_vendor_units": item.get("p"),
            "active_speed_vendor_units": item.get("s"),
        }
    return result


class NiceHashContextCollector:
    SHA256_ALGORITHM_IDS = (1, 35)

    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient()

    def collect(self, *, now: datetime | None = None) -> Observation:
        observed_at = now or utc_now()
        try:
            current = _nicehash_algorithms(self.http.get(NICEHASH_CURRENT_URL))
            history = _nicehash_algorithms(self.http.get(NICEHASH_24H_URL))
            buy_info_payload = _require_mapping(
                self.http.get(NICEHASH_BUY_INFO_URL), "NiceHash buy info"
            )
            raw_buy_algorithms = buy_info_payload.get("miningAlgorithms", [])
            buy_by_id: dict[int, Mapping[str, Any]] = {}
            if isinstance(raw_buy_algorithms, list):
                for item in raw_buy_algorithms:
                    if not isinstance(item, Mapping):
                        continue
                    raw_id = item.get("algo", item.get("algorithm"))
                    if raw_id is not None:
                        try:
                            buy_by_id[int(raw_id)] = item
                        except (TypeError, ValueError):
                            continue

            algorithms: list[dict[str, Any]] = []
            for algorithm_id in self.SHA256_ALGORITHM_IDS:
                if algorithm_id not in current:
                    continue
                buy = buy_by_id.get(algorithm_id, {})
                algorithms.append(
                    {
                        **current[algorithm_id],
                        "24h_active_speed_vendor_units": history.get(
                            algorithm_id, {}
                        ).get("active_speed_vendor_units"),
                        "24h_paying_vendor_units": history.get(
                            algorithm_id, {}
                        ).get("paying_vendor_units"),
                        "name": buy.get("name"),
                        "speed_unit": buy.get("speed_text"),
                        "speed_multiplier": buy.get("multi"),
                        "price_multiplier": buy.get("price_multi"),
                        "enabled_markets": buy.get("enabledHashpowerMarkets"),
                    }
                )
            if not algorithms:
                raise ValueError("NiceHash response has no SHA256 algorithms")
            return Observation.ok(
                "nicehash_sha256_context",
                "api2.nicehash.com/public",
                {"algorithms": algorithms},
                observed_at=observed_at,
                metadata={
                    "description": (
                        "Active delivered marketplace speed, not unused rentable capacity."
                    )
                },
            )
        except Exception as exc:
            return Observation.failed(
                "nicehash_sha256_context",
                "api2.nicehash.com/public",
                str(exc),
                observed_at=observed_at,
            )


class BlackoutNewsCollector:
    LOCATIONS: Sequence[str] = (
        "China",
        "Kazakhstan",
        "Uzbekistan",
        "Georgia",
        "Iceland",
        "Texas",
        "Washington",
        "Sichuan",
        "Inner Mongolia",
        "Xinjiang",
        "Quebec",
        "Alberta",
        "Irkutsk",
    )
    KEYWORDS: Sequence[str] = (
        "blackout",
        "power outage",
        "electricity shortage",
        "grid emergency",
        "mining ban",
    )

    def __init__(
        self,
        api_key: str | None,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.http = http or JsonHttpClient()

    def collect(
        self,
        *,
        now: datetime | None = None,
        days_back: int = 2,
    ) -> Observation:
        observed_at = now or utc_now()
        if not self.api_key:
            return self._collect_gdelt(observed_at, days_back=max(1, days_back))
        try:
            keyword_query = " OR ".join(f'"{keyword}"' for keyword in self.KEYWORDS)
            location_query = " OR ".join(f'"{location}"' for location in self.LOCATIONS)
            response = _require_mapping(
                self.http.get(
                    NEWS_API_URL,
                    params={
                        "q": f"({keyword_query}) AND ({location_query})",
                        "from": (
                            observed_at - timedelta(days=max(1, days_back))
                        ).date().isoformat(),
                        "to": observed_at.date().isoformat(),
                        "language": "en",
                        "sortBy": "publishedAt",
                        "pageSize": 100,
                        "apiKey": self.api_key,
                    },
                ),
                "blackout news",
            )
            raw_articles = response.get("articles")
            if not isinstance(raw_articles, list):
                raise ValueError("news response has no articles list")

            articles: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            for item in raw_articles:
                if not isinstance(item, Mapping):
                    continue
                url = str(item.get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                text = " ".join(
                    str(item.get(field) or "") for field in ("title", "description")
                ).lower()
                matched_locations = [
                    location
                    for location in self.LOCATIONS
                    if location.lower() in text
                ]
                matched_keywords = [
                    keyword
                    for keyword in self.KEYWORDS
                    if keyword.lower() in text
                ]
                if not matched_locations or not matched_keywords:
                    continue
                source = item.get("source")
                source_name = (
                    str(source.get("name") or "")
                    if isinstance(source, Mapping)
                    else ""
                )
                articles.append(
                    {
                        "title": str(item.get("title") or ""),
                        "url": url,
                        "published_at": item.get("publishedAt"),
                        "source": source_name,
                        "locations": matched_locations,
                        "keywords": matched_keywords,
                    }
                )
            return Observation.ok(
                "blackout_news",
                "newsapi.org",
                {
                    "article_count": len(articles),
                    "articles": articles,
                    "days_back": max(1, days_back),
                },
                observed_at=observed_at,
                metadata={
                    "description": (
                        "Low-confidence contextual news; never direct proof of an attack."
                    )
                },
            )
        except Exception as exc:
            return Observation.failed(
                "blackout_news",
                "newsapi.org",
                str(exc),
                observed_at=observed_at,
            )

    def _collect_gdelt(
        self,
        observed_at: datetime,
        *,
        days_back: int,
    ) -> Observation:
        try:
            response = _require_mapping(
                self.http.get(
                    GDELT_DOC_URL,
                    params={
                        "query": (
                            '("power outage" OR blackout OR "grid emergency") '
                            '(bitcoin OR "crypto mining")'
                        ),
                        "mode": "artlist",
                        "format": "json",
                        "maxrecords": 50,
                        "timespan": f"{days_back}d",
                        "sort": "datedesc",
                    },
                ),
                "GDELT blackout news",
            )
            raw_articles = response.get("articles")
            if not isinstance(raw_articles, list):
                raise ValueError("GDELT response has no articles list")
            articles: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in raw_articles:
                if not isinstance(item, Mapping):
                    continue
                url = str(item.get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                text = str(item.get("title") or "").lower()
                matched_locations = [
                    location
                    for location in self.LOCATIONS
                    if location.lower() in text
                ]
                matched_keywords = [
                    keyword
                    for keyword in self.KEYWORDS
                    if keyword.lower() in text
                ]
                if not matched_locations or not matched_keywords:
                    continue
                articles.append(
                    {
                        "title": str(item.get("title") or ""),
                        "url": url,
                        "published_at": item.get("seendate"),
                        "source": item.get("domain"),
                        "source_country": item.get("sourcecountry"),
                        "locations": matched_locations,
                        "keywords": matched_keywords,
                    }
                )
            return Observation.ok(
                "blackout_news",
                "gdeltproject.org/doc-api",
                {
                    "article_count": len(articles),
                    "articles": articles,
                    "days_back": days_back,
                },
                observed_at=observed_at,
                metadata={
                    "description": (
                        "Low-confidence contextual news; publisher country is not incident location."
                    )
                },
            )
        except Exception as exc:
            return Observation.failed(
                "blackout_news",
                "gdeltproject.org/doc-api",
                str(exc),
                observed_at=observed_at,
            )


def collect_research_context(
    block_height: int | None,
    *,
    now: datetime | None = None,
    halving_window_blocks: int = 1008,
) -> Observation:
    observed_at = now or utc_now()
    hour = observed_at.astimezone(timezone.utc).hour
    in_research_time_window = 2 <= hour < 4 or 10 <= hour < 12

    value: dict[str, Any] = {
        "utc_hour": hour,
        "in_research_time_window": in_research_time_window,
        "halving_window_blocks": halving_window_blocks,
        "block_height": block_height,
        "blocks_until_halving": None,
        "blocks_since_halving": None,
        "near_halving": None,
    }
    partial = block_height is None
    if block_height is not None and block_height >= 0:
        blocks_since = block_height % 210_000
        blocks_until = 210_000 - blocks_since
        value.update(
            {
                "blocks_until_halving": blocks_until,
                "blocks_since_halving": blocks_since,
                "near_halving": min(blocks_since, blocks_until)
                <= halving_window_blocks,
            }
        )
    return Observation.ok(
        "research_context",
        "local-policy",
        value,
        observed_at=observed_at,
        partial=partial,
        metadata={
            "description": (
                "Paper-derived context only; these factors cannot establish a chain attack."
            )
        },
    )
