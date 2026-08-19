#!/usr/bin/env python3
"""Fetch per-session token and cost records from AgentCore Runtime CloudWatch logs.

Examples:
  python3 extract_model_usage.py
  python3 extract_model_usage.py --start '2026-08-19 17:25'
  python3 extract_model_usage.py --session-id 5a9cf2ee-ab4f-4810-a72e-d15646cb1a30
  python3 extract_model_usage.py --csv august-token-cost.csv

The script uses the normal boto3/AWS credential chain. It needs only
logs:FilterLogEvents for the selected CloudWatch log group.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError


DEFAULT_REGION = "ap-southeast-1"
DEFAULT_LOG_GROUP = (
    "/aws/bedrock-agentcore/runtimes/Strands_runtime-mk6uFHBu9d-DEFAULT"
)
SGT = ZoneInfo("Asia/Singapore")
MARKER = "MODEL_USAGE "
CSV_COLUMNS = (
    "timestamp_sgt",
    "session_id",
    "succeeded",
    "duration_seconds",
    "input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "total_input_tokens",
    "output_tokens",
    "total_tokens_reported",
    "estimated_cost_usd",
    "estimated_cost_input_usd",
    "estimated_cost_output_usd",
    "estimated_cost_cache_read_usd",
    "estimated_cost_cache_write_usd",
    "model_id",
    "log_stream",
)


def parse_time(value: str) -> datetime:
    """Parse an ISO timestamp; naïve values are interpreted as Singapore time."""
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Use ISO time, for example '2026-08-19 17:25' or "
            "'2026-08-19T17:25:00+08:00'."
        ) from error
    return parsed.replace(tzinfo=SGT) if parsed.tzinfo is None else parsed


def usage_record(event: dict) -> dict | None:
    """Extract the safe model-usage JSON from one CloudWatch event."""
    message = event.get("message", "")
    if MARKER not in message:
        return None
    try:
        usage = json.loads(message.split(MARKER, 1)[1])
    except json.JSONDecodeError:
        return None
    if usage.get("event") != "model_usage":
        return None

    timestamp = datetime.fromtimestamp(event["timestamp"] / 1000, tz=SGT)
    return {
        "timestamp_sgt": timestamp.isoformat(),
        "session_id": usage.get("session_id"),
        "succeeded": usage.get("succeeded"),
        "duration_seconds": round((usage.get("duration_ms", 0) or 0) / 1000, 3),
        "input_tokens": usage.get("input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_write_input_tokens": usage.get("cache_write_input_tokens", 0),
        "total_input_tokens": usage.get("total_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens_reported": usage.get("total_tokens_reported", 0),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "estimated_cost_breakdown_usd": usage.get("estimated_cost_breakdown_usd", {}),
        "model_id": usage.get("model_id"),
        "log_stream": event.get("logStreamName"),
    }


def default_csv_path(start: datetime, end: datetime) -> Path:
    """Return a filesystem-safe filename that describes the queried SGT range."""
    def format_timestamp(value: datetime) -> str:
        return value.astimezone(SGT).strftime("%Y%m%dT%H%M%S%Z")

    return Path(
        "agentcore_token_cost_"
        f"{format_timestamp(start)}_to_{format_timestamp(end)}.csv"
    )


def write_csv(records: list[dict], path: Path) -> None:
    """Write one flattened usage record per row for Excel and other CSV readers."""
    with path.open("x", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for record in records:
            costs = record.get("estimated_cost_breakdown_usd", {})
            row = {column: record.get(column) for column in CSV_COLUMNS}
            row.update(
                {
                    "estimated_cost_input_usd": costs.get("input"),
                    "estimated_cost_output_usd": costs.get("output"),
                    "estimated_cost_cache_read_usd": costs.get("cache_read"),
                    "estimated_cost_cache_write_usd": costs.get("cache_write"),
                }
            )
            writer.writerow(row)


def get_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"CloudWatch region (default: {DEFAULT_REGION})",
    )
    parser.add_argument("--profile", help="Optional AWS shared-config profile name")
    parser.add_argument("--log-group", default=DEFAULT_LOG_GROUP)
    parser.add_argument(
        "--minutes",
        type=int,
        default=120,
        help="Look back this many minutes when --start is omitted (default: 120)",
    )
    parser.add_argument("--start", type=parse_time, help="Start time; naïve values are SGT")
    parser.add_argument("--end", type=parse_time, help="End time; defaults to now")
    parser.add_argument("--session-id", help="Return only this AgentCore runtime session")
    parser.add_argument(
        "--csv",
        type=Path,
        help=(
            "Output CSV filename. Defaults to "
            "agentcore_token_cost_[SGT time range].csv in the current directory"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = get_arguments()
    if args.minutes <= 0:
        raise SystemExit("--minutes must be positive")

    end = args.end or datetime.now(tz=SGT)
    default_minutes = 24 * 60 if args.session_id else args.minutes
    start = args.start or end - timedelta(minutes=default_minutes)
    if end <= start:
        raise SystemExit("--end must be later than --start")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    logs = session.client("logs")
    records: list[dict] = []
    try:
        paginator = logs.get_paginator("filter_log_events")
        for page in paginator.paginate(
            logGroupName=args.log_group,
            startTime=round(start.timestamp() * 1000),
            endTime=round(end.timestamp() * 1000),
            PaginationConfig={"PageSize": 10_000},
        ):
            for event in page.get("events", []):
                record = usage_record(event)
                if record and (
                    not args.session_id or record["session_id"] == args.session_id
                ):
                    records.append(record)
    except (BotoCoreError, ClientError) as error:
        raise SystemExit(f"CloudWatch query failed: {error}") from error

    if not records:
        raise SystemExit(
            "No completed MODEL_USAGE records found. The session may still be "
            "running; otherwise widen --minutes or pass --start/--session-id."
        )

    records.sort(key=lambda item: item["timestamp_sgt"])
    output_path = args.csv or default_csv_path(start, end)
    try:
        write_csv(records, output_path)
    except FileExistsError as error:
        raise SystemExit(
            f"Refusing to overwrite existing CSV: {output_path}. "
            "Choose another filename with --csv."
        ) from error
    except OSError as error:
        raise SystemExit(f"Could not write CSV {output_path}: {error}") from error

    print(f"Wrote {len(records)} record(s) to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
