from __future__ import annotations

import argparse
import json
import time
from typing import Mapping, Sequence

from .ai import AIRecommendation, generate_ai_recommendation
from .report import render_report
from .service import AlertGate, MonitorService
from .settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="block-detector",
        description=(
            "Display a Bitcoin chain-risk score, supporting evidence, and "
            "defensive recommendations."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    report = subparsers.add_parser(
        "report",
        help="Display one complete human-readable risk report",
    )
    report.add_argument(
        "--no-ai",
        action="store_true",
        help="Do not request the optional OpenAI advisory",
    )
    report.add_argument(
        "--json",
        action="store_true",
        help="Emit the report snapshot and AI advisory as JSON",
    )

    snapshot = subparsers.add_parser("snapshot", help="Collect one JSON snapshot")
    snapshot.add_argument("--compact", action="store_true", help="Emit compact JSON")

    monitor = subparsers.add_parser("monitor", help="Continuously monitor and alert")
    monitor.add_argument("--interval", type=float, help="Polling interval in seconds")
    monitor.add_argument("--once", action="store_true", help="Run one monitor cycle")
    monitor.add_argument(
        "--all-snapshots",
        action="store_true",
        help="Print every snapshot, not only alert transitions",
    )
    return parser


def _print_json(value: object, *, compact: bool = False) -> None:
    print(
        json.dumps(
            value,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
            sort_keys=True,
        )
    )


def snapshot_main(*, compact: bool = False) -> int:
    snapshot = MonitorService().collect()
    _print_json(snapshot, compact=compact)
    return 0


def report_main(*, use_ai: bool = True, as_json: bool = False) -> int:
    snapshot = MonitorService().collect()
    recommendation = (
        generate_ai_recommendation(snapshot)
        if use_ai
        else AIRecommendation.disabled()
    )
    if as_json:
        value = dict(snapshot)
        value["ai_recommendation"] = recommendation.to_dict()
        _print_json(value)
    else:
        print(render_report(snapshot, recommendation))
    return 0


def _print_alert(snapshot: Mapping[str, object]) -> None:
    assessment = snapshot.get("assessment")
    if not isinstance(assessment, Mapping):
        return
    level = str(assessment.get("level", "unknown")).upper()
    score = assessment.get("evidence_score", "N/A")
    print(f"[{level} score={score}/100] {assessment.get('summary', '')}")
    reasons = assessment.get("reasons", [])
    if isinstance(reasons, list):
        for reason in reasons:
            print(f"  - {reason}")
    print(f"  data quality: {assessment.get('data_quality', 'unknown')}")


def monitor_main(
    *,
    interval: float | None = None,
    once: bool = False,
    all_snapshots: bool = False,
) -> int:
    settings = Settings.from_env()
    service = MonitorService(settings)
    gate = AlertGate(settings.repeat_alert_seconds)
    poll_seconds = interval if interval is not None else settings.poll_seconds
    if poll_seconds <= 0:
        raise ValueError("polling interval must be positive")

    try:
        while True:
            snapshot = service.collect()
            if all_snapshots:
                _print_json(snapshot)
            elif gate.should_emit(snapshot):
                _print_alert(snapshot)
            else:
                assessment = snapshot.get("assessment")
                level = (
                    assessment.get("level", "unknown")
                    if isinstance(assessment, Mapping)
                    else "unknown"
                )
                score = (
                    assessment.get("evidence_score", "N/A")
                    if isinstance(assessment, Mapping)
                    else "N/A"
                )
                print(
                    f"{snapshot['generated_at']} level={level} score={score}/100"
                )
            if once:
                return 0
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("Monitoring stopped.")
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        return report_main()
    if args.command == "report":
        return report_main(use_ai=not args.no_ai, as_json=args.json)
    if args.command == "snapshot":
        return snapshot_main(compact=bool(getattr(args, "compact", False)))
    if args.command == "monitor":
        return monitor_main(
            interval=args.interval,
            once=args.once,
            all_snapshots=args.all_snapshots,
        )
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
