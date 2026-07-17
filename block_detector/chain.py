from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

from .http import JsonHttpClient
from .models import Observation, utc_now
from .settings import Settings


class BitcoinRPCError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChainTip:
    height: int
    block_hash: str
    branch_length: int
    status: str


@dataclass(frozen=True)
class NodeSnapshot:
    node_id: str
    observed_at: datetime
    chain: str
    height: int
    headers: int
    best_hash: str
    previous_hash: str | None
    block_time: int
    chainwork: int
    initial_block_download: bool
    verification_progress: float
    pruned: bool
    warnings: str
    tips: tuple[ChainTip, ...]

    def to_state(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            "height": self.height,
            "best_hash": self.best_hash,
            "previous_hash": self.previous_hash,
            "chainwork": self.chainwork,
        }


@dataclass(frozen=True)
class PreviousTip:
    node_id: str
    observed_at: str
    height: int
    best_hash: str
    previous_hash: str | None
    chainwork: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PreviousTip":
        return cls(
            node_id=str(value["node_id"]),
            observed_at=str(value["observed_at"]),
            height=int(value["height"]),
            best_hash=str(value["best_hash"]),
            previous_hash=(
                str(value["previous_hash"]) if value.get("previous_hash") else None
            ),
            chainwork=int(value["chainwork"]),
        )


class BitcoinRPCClient:
    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        node_id: str | None = None,
        http: JsonHttpClient | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Bitcoin RPC URL must be an http(s) URL with a host")

        url_user = unquote(parsed.username) if parsed.username else None
        url_password = unquote(parsed.password) if parsed.password else None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        self.endpoint = urlunsplit(
            (parsed.scheme, netloc, parsed.path or "/", parsed.query, "")
        )
        self.auth = (
            (username or url_user, password or url_password)
            if (username or url_user) is not None
            else None
        )
        self.node_id = node_id or netloc
        self.http = http or JsonHttpClient()
        self._request_id = 0

    def call(self, method: str, params: Iterable[Any] = ()) -> Any:
        self._request_id += 1
        payload = self.http.post(
            self.endpoint,
            json={
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": list(params),
            },
            auth=self.auth,
        )
        if not isinstance(payload, Mapping):
            raise BitcoinRPCError(f"{self.node_id}: malformed RPC response")
        error = payload.get("error")
        if error:
            if isinstance(error, Mapping):
                message = error.get("message", "unknown RPC error")
                code = error.get("code")
                raise BitcoinRPCError(f"{self.node_id}: RPC {code}: {message}")
            raise BitcoinRPCError(f"{self.node_id}: {error}")
        if "result" not in payload:
            raise BitcoinRPCError(f"{self.node_id}: RPC response has no result")
        return payload["result"]

    def snapshot(self, *, attempts: int = 2) -> NodeSnapshot:
        for attempt in range(attempts):
            first_info = self.call("getblockchaininfo")
            if not isinstance(first_info, Mapping):
                raise BitcoinRPCError(
                    f"{self.node_id}: invalid getblockchaininfo response"
                )
            best_hash = str(first_info["bestblockhash"])
            header = self.call("getblockheader", (best_hash,))
            raw_tips = self.call("getchaintips")
            second_info = self.call("getblockchaininfo")
            if (
                isinstance(second_info, Mapping)
                and str(second_info.get("bestblockhash")) == best_hash
            ):
                break
            if attempt + 1 == attempts:
                raise BitcoinRPCError(
                    f"{self.node_id}: chain tip changed during snapshot"
                )
        else:  # pragma: no cover - the loop always breaks or raises
            raise BitcoinRPCError(f"{self.node_id}: could not take snapshot")

        if not isinstance(header, Mapping) or not isinstance(raw_tips, list):
            raise BitcoinRPCError(f"{self.node_id}: malformed chain snapshot")
        tips = tuple(
            ChainTip(
                height=int(item["height"]),
                block_hash=str(item["hash"]),
                branch_length=int(item["branchlen"]),
                status=str(item["status"]),
            )
            for item in raw_tips
            if isinstance(item, Mapping)
        )
        return NodeSnapshot(
            node_id=self.node_id,
            observed_at=utc_now(),
            chain=str(first_info["chain"]),
            height=int(first_info["blocks"]),
            headers=int(first_info["headers"]),
            best_hash=best_hash,
            previous_hash=(
                str(header["previousblockhash"])
                if header.get("previousblockhash")
                else None
            ),
            block_time=int(header["time"]),
            chainwork=int(str(first_info["chainwork"]), 16),
            initial_block_download=bool(first_info["initialblockdownload"]),
            verification_progress=float(first_info["verificationprogress"]),
            pruned=bool(first_info["pruned"]),
            warnings=str(first_info.get("warnings", "")),
            tips=tips,
        )

    def active_hash_at_height(self, height: int) -> str:
        return str(self.call("getblockhash", (height,)))

    def find_detached_depth(
        self,
        previous: PreviousTip,
        current: NodeSnapshot,
        *,
        max_depth: int,
    ) -> int | None:
        if previous.best_hash == current.best_hash:
            return 0

        try:
            if previous.height <= current.height:
                active_at_previous = self.active_hash_at_height(previous.height)
                if active_at_previous == previous.best_hash:
                    return 0
        except (BitcoinRPCError, KeyError, TypeError, ValueError):
            pass

        cursor = previous.best_hash
        detached = 0
        while cursor and detached <= max_depth:
            try:
                header = self.call("getblockheader", (cursor,))
                if not isinstance(header, Mapping):
                    return None
                height = int(header["height"])
                if height <= current.height:
                    active_hash = self.active_hash_at_height(height)
                    if active_hash == cursor:
                        return detached
                cursor = (
                    str(header["previousblockhash"])
                    if header.get("previousblockhash")
                    else ""
                )
                detached += 1
            except (BitcoinRPCError, KeyError, TypeError, ValueError):
                return None
        return None


class ChainStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def load(self) -> dict[str, PreviousTip]:
        try:
            nodes = self._load_payload().get("nodes", {})
            if not isinstance(nodes, Mapping):
                return {}
            return {
                str(node_id): PreviousTip.from_mapping(value)
                for node_id, value in nodes.items()
                if isinstance(value, Mapping)
            }
        except (KeyError, TypeError, ValueError):
            return {}

    def load_wallet_cursors(self) -> dict[str, str]:
        cursors = self._load_payload().get("wallet_cursors", {})
        if not isinstance(cursors, Mapping):
            return {}
        return {
            str(node_id): str(block_hash)
            for node_id, block_hash in cursors.items()
            if block_hash
        }

    def save(
        self,
        snapshots: Iterable[NodeSnapshot],
        *,
        wallet_cursors: Mapping[str, str] | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._load_payload()
        existing_nodes = existing.get("nodes", {})
        nodes = (
            dict(existing_nodes) if isinstance(existing_nodes, Mapping) else {}
        )
        nodes.update(
            {snapshot.node_id: snapshot.to_state() for snapshot in snapshots}
        )
        existing_cursors = existing.get("wallet_cursors", {})
        cursors = (
            dict(existing_cursors)
            if isinstance(existing_cursors, Mapping)
            else {}
        )
        if wallet_cursors:
            cursors.update(wallet_cursors)
        payload = {
            "version": 1,
            "nodes": nodes,
            "wallet_cursors": cursors,
        }
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def _snapshot_dict(snapshot: NodeSnapshot) -> dict[str, Any]:
    result = asdict(snapshot)
    result["observed_at"] = snapshot.observed_at.isoformat()
    result["chainwork"] = f"{snapshot.chainwork:x}"
    return result


class ChainMonitor:
    def __init__(
        self,
        clients: Iterable[BitcoinRPCClient],
        *,
        state_store: ChainStateStore,
        expected_network: str = "main",
        minimum_healthy_nodes: int = 2,
        max_reorg_search_depth: int = 144,
        monitor_wallet_transactions: bool = False,
        wallet_target_confirmations: int = 6,
    ) -> None:
        self.clients = tuple(clients)
        self.state_store = state_store
        self.expected_network = expected_network
        self.minimum_healthy_nodes = minimum_healthy_nodes
        self.max_reorg_search_depth = max_reorg_search_depth
        self.monitor_wallet_transactions = monitor_wallet_transactions
        self.wallet_target_confirmations = wallet_target_confirmations

    def collect(self) -> Observation:
        if not self.clients:
            return Observation.unavailable(
                "chain_signals",
                "bitcoin-core",
                "BITCOIN_RPC_URLS is not configured",
            )

        previous = self.state_store.load()
        healthy: list[NodeSnapshot] = []
        errors: dict[str, str] = {}
        excluded: dict[str, str] = {}
        client_by_id = {client.node_id: client for client in self.clients}

        for client in self.clients:
            try:
                snapshot = client.snapshot()
                if snapshot.chain != self.expected_network:
                    excluded[snapshot.node_id] = (
                        f"wrong network: {snapshot.chain}, expected {self.expected_network}"
                    )
                elif snapshot.initial_block_download:
                    excluded[snapshot.node_id] = "node is in initial block download"
                elif snapshot.headers - snapshot.height > 2:
                    excluded[snapshot.node_id] = "node is materially behind its headers"
                else:
                    healthy.append(snapshot)
            except Exception as exc:
                errors[client.node_id] = str(exc)

        if not healthy:
            return Observation.unavailable(
                "chain_signals",
                "bitcoin-core",
                "No healthy Bitcoin Core nodes were available",
                metadata={"errors": errors, "excluded": excluded},
            )

        reorgs: list[dict[str, Any]] = []
        quality_events: list[dict[str, Any]] = []
        for snapshot in healthy:
            old = previous.get(snapshot.node_id)
            if old is None or old.best_hash == snapshot.best_hash:
                continue
            client = client_by_id[snapshot.node_id]
            depth = client.find_detached_depth(
                old,
                snapshot,
                max_depth=self.max_reorg_search_depth,
            )
            if snapshot.chainwork <= old.chainwork:
                quality_events.append(
                    {
                        "node_id": snapshot.node_id,
                        "kind": "chainwork_not_increasing",
                        "previous_height": old.height,
                        "current_height": snapshot.height,
                    }
                )
            if depth is None:
                quality_events.append(
                    {
                        "node_id": snapshot.node_id,
                        "kind": "tip_discontinuity_unknown_depth",
                        "previous_hash": old.best_hash,
                        "current_hash": snapshot.best_hash,
                    }
                )
            elif depth > 0:
                reorgs.append(
                    {
                        "node_id": snapshot.node_id,
                        "detached_depth": depth,
                        "previous_height": old.height,
                        "current_height": snapshot.height,
                        "previous_hash": old.best_hash,
                        "current_hash": snapshot.best_hash,
                    }
                )

        common_height = min(snapshot.height for snapshot in healthy)
        hashes_at_common_height: dict[str, str] = {}
        for snapshot in healthy:
            try:
                hashes_at_common_height[snapshot.node_id] = client_by_id[
                    snapshot.node_id
                ].active_hash_at_height(common_height)
            except Exception as exc:
                errors[snapshot.node_id] = str(exc)

        distinct_common_hashes = set(hashes_at_common_height.values())
        node_divergence = len(distinct_common_hashes) > 1
        valid_forks = [
            {
                "node_id": snapshot.node_id,
                **asdict(tip),
            }
            for snapshot in healthy
            for tip in snapshot.tips
            if tip.status in {"valid-fork", "valid-headers", "headers-only"}
            and tip.branch_length > 0
        ]
        max_valid_fork = max(
            (
                int(item["branch_length"])
                for item in valid_forks
                if item["status"] == "valid-fork"
            ),
            default=0,
        )
        max_reorg_depth = max(
            (int(item["detached_depth"]) for item in reorgs), default=0
        )
        common_height_comparison_count = len(hashes_at_common_height)
        quorum_met = (
            common_height_comparison_count >= self.minimum_healthy_nodes
        )
        quality_event_kinds = sorted(
            {
                str(item.get("kind"))
                for item in quality_events
                if item.get("kind")
            }
        )
        untrusted_state_node_ids = {
            str(item.get("node_id"))
            for item in quality_events
            if item.get("node_id")
        }

        wallet_removed_transactions: list[dict[str, Any]] = []
        wallet_cursors = self.state_store.load_wallet_cursors()
        if self.monitor_wallet_transactions:
            for snapshot in healthy:
                client = client_by_id[snapshot.node_id]
                cursor = wallet_cursors.get(snapshot.node_id)
                if cursor is None:
                    wallet_cursors[snapshot.node_id] = snapshot.best_hash
                    continue
                try:
                    wallet_result = client.call(
                        "listsinceblock",
                        (
                            cursor,
                            self.wallet_target_confirmations,
                            False,
                            True,
                        ),
                    )
                    if not isinstance(wallet_result, Mapping):
                        raise BitcoinRPCError("listsinceblock response is malformed")
                    removed = wallet_result.get("removed", [])
                    if isinstance(removed, list):
                        for item in removed:
                            if not isinstance(item, Mapping):
                                continue
                            wallet_removed_transactions.append(
                                {
                                    "node_id": snapshot.node_id,
                                    "txid": item.get("txid"),
                                    "category": item.get("category"),
                                    "amount": item.get("amount"),
                                    "confirmations": item.get("confirmations"),
                                    "blockhash": item.get("blockhash"),
                                    "wallet_conflicts": item.get(
                                        "walletconflicts", []
                                    ),
                                }
                            )
                    lastblock = wallet_result.get("lastblock")
                    if lastblock:
                        wallet_cursors[snapshot.node_id] = str(lastblock)
                except Exception as exc:
                    errors[f"{snapshot.node_id}:wallet"] = str(exc)

        wallet_at_risk_count = sum(
            1
            for item in wallet_removed_transactions
            if isinstance(item.get("confirmations"), (int, float))
            and float(item["confirmations"]) <= 0
        )
        trusted_snapshots = [
            snapshot
            for snapshot in healthy
            if snapshot.node_id not in untrusted_state_node_ids
        ]
        trusted_wallet_cursors = {
            node_id: cursor
            for node_id, cursor in wallet_cursors.items()
            if node_id not in untrusted_state_node_ids
        }
        self.state_store.save(
            trusted_snapshots,
            wallet_cursors=trusted_wallet_cursors,
        )
        value = {
            "healthy_node_count": len(healthy),
            "configured_node_count": len(self.clients),
            "minimum_healthy_nodes": self.minimum_healthy_nodes,
            "quorum_met": quorum_met,
            "common_height": common_height,
            "common_height_comparison_count": common_height_comparison_count,
            "hashes_at_common_height": hashes_at_common_height,
            "node_divergence": node_divergence,
            "height_spread": max(item.height for item in healthy)
            - min(item.height for item in healthy),
            "max_reorg_depth": max_reorg_depth,
            "reorgs": reorgs,
            "max_valid_fork_branch_length": max_valid_fork,
            "competing_tips": valid_forks,
            "quality_events": quality_events,
            "quality_event_count": len(quality_events),
            "quality_event_kinds": quality_event_kinds,
            "state_update_skipped_count": len(untrusted_state_node_ids),
            "state_update_skipped_nodes": sorted(untrusted_state_node_ids),
            "wallet_monitoring_enabled": self.monitor_wallet_transactions,
            "wallet_removed_transactions": wallet_removed_transactions,
            "wallet_removed_at_risk_count": wallet_at_risk_count,
            "nodes": [_snapshot_dict(snapshot) for snapshot in healthy],
        }
        partial = bool(errors or excluded or not quorum_met or quality_events)
        return Observation.ok(
            "chain_signals",
            "bitcoin-core",
            value,
            partial=partial,
            metadata={"errors": errors, "excluded": excluded},
        )


def build_chain_monitor(settings: Settings) -> ChainMonitor:
    clients = [
        BitcoinRPCClient(
            url,
            username=settings.bitcoin_rpc_user,
            password=settings.bitcoin_rpc_password,
            node_id=f"node-{index + 1}:{urlsplit(url).hostname or 'unknown'}",
            http=JsonHttpClient(timeout=settings.request_timeout),
        )
        for index, url in enumerate(settings.bitcoin_rpc_urls)
    ]
    return ChainMonitor(
        clients,
        state_store=ChainStateStore(settings.state_path),
        expected_network=settings.bitcoin_network,
        minimum_healthy_nodes=settings.minimum_healthy_nodes,
        max_reorg_search_depth=settings.max_reorg_search_depth,
        monitor_wallet_transactions=settings.monitor_wallet_transactions,
        wallet_target_confirmations=settings.wallet_target_confirmations,
    )
