#!/usr/bin/env python3
"""Claude Code usage log → OTLP metrics exporter.

Reads ~/.claude/projects/**/*.jsonl, extracts token usage from assistant
messages, and ships them as OTLP counters to a collector endpoint.

Usage:
    python claude_exporter.py [options]

    OTLP_ENDPOINT=http://your-server:4318 CLAUDE_USER=alice python claude_exporter.py
"""

import argparse
import json
import os
import socket
import time
from pathlib import Path

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

# Pricing per 1M tokens (USD) — as of 2026-05
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 15.0, "output": 75.0,
        "cache_read": 1.50, "cache_creation": 18.75,
    },
    "claude-sonnet-4-6": {
        "input": 3.0, "output": 15.0,
        "cache_read": 0.30, "cache_creation": 3.75,
    },
    "claude-haiku-4-5": {
        "input": 0.80, "output": 4.0,
        "cache_read": 0.08, "cache_creation": 1.0,
    },
}
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75}


def model_pricing(model: str) -> dict[str, float]:
    for prefix, p in PRICING.items():
        if model.startswith(prefix):
            return p
    return _DEFAULT_PRICING


def state_path() -> Path:
    return Path.home() / ".claude" / "exporter_state.json"


def load_state() -> dict:
    p = state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"offsets": {}}


def save_state(state: dict) -> None:
    state_path().write_text(json.dumps(state, indent=2))


def iter_new_records(jsonl_path: Path, offset: int):
    """Yield (new_offset, record_or_None) for each new line after offset."""
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(offset)
            for raw in f:
                new_offset = f.tell()
                try:
                    yield new_offset, json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    yield new_offset, None
    except OSError:
        pass


def project_name(cwd: str) -> str:
    return Path(cwd).name if cwd else "unknown"


def poll_once(
    claude_dir: Path,
    state: dict,
    counters: dict,
    user_label: str,
) -> int:
    """Scan for new log records and update counters. Returns count processed."""
    processed = 0
    offsets = state.setdefault("offsets", {})

    for jsonl_file in sorted(claude_dir.glob("projects/**/*.jsonl")):
        key = str(jsonl_file)
        current_offset = offsets.get(key, 0)
        last_offset = current_offset

        for new_offset, record in iter_new_records(jsonl_file, current_offset):
            last_offset = new_offset
            if not record or record.get("type") != "assistant":
                continue

            usage = record.get("message", {}).get("usage", {})
            if not usage:
                continue

            model = record.get("message", {}).get("model", "unknown")
            cwd = record.get("cwd", "")
            entrypoint = record.get("entrypoint", "unknown")

            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            cache_read = int(usage.get("cache_read_input_tokens", 0))
            cache_creation = int(usage.get("cache_creation_input_tokens", 0))

            p = model_pricing(model)
            cost_usd = (
                input_tokens * p["input"]
                + output_tokens * p["output"]
                + cache_read * p["cache_read"]
                + cache_creation * p["cache_creation"]
            ) / 1_000_000

            attrs = {
                "model": model,
                "project": project_name(cwd),
                "user": user_label,
                "entrypoint": entrypoint,
            }

            counters["input_tokens"].add(input_tokens, attrs)
            counters["output_tokens"].add(output_tokens, attrs)
            counters["cache_read_tokens"].add(cache_read, attrs)
            counters["cache_creation_tokens"].add(cache_creation, attrs)
            counters["cost_usd"].add(cost_usd, attrs)
            counters["requests"].add(1, attrs)
            processed += 1

        if last_offset > current_offset:
            offsets[key] = last_offset

    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code → OTLP usage exporter")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("OTLP_ENDPOINT", "http://localhost:4318"),
        help="OTLP HTTP endpoint base URL (default: http://localhost:4318)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("EXPORT_INTERVAL", "30")),
        help="Poll interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("CLAUDE_USER", socket.gethostname()),
        help="User label attached to all metrics (default: hostname)",
    )
    parser.add_argument(
        "--claude-dir",
        default=str(Path.home() / ".claude"),
        help="Path to Claude config directory",
    )
    args = parser.parse_args()

    claude_dir = Path(args.claude_dir)
    user_label = args.user
    otlp_url = args.endpoint.rstrip("/") + "/v1/metrics"

    resource = Resource(attributes={
        SERVICE_NAME: "claude-code-exporter",
        "host.name": socket.gethostname(),
        "claude.user": user_label,
    })

    exporter = OTLPMetricExporter(endpoint=otlp_url)
    reader = PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=args.interval * 1000,
    )
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)

    meter = metrics.get_meter("claude_code", version="1.0.0")
    counters = {
        "input_tokens": meter.create_counter(
            "claude_code_input_tokens",
            unit="tokens",
            description="Input tokens consumed (prompt)",
        ),
        "output_tokens": meter.create_counter(
            "claude_code_output_tokens",
            unit="tokens",
            description="Output tokens generated (completion)",
        ),
        "cache_read_tokens": meter.create_counter(
            "claude_code_cache_read_tokens",
            unit="tokens",
            description="Tokens served from prompt cache",
        ),
        "cache_creation_tokens": meter.create_counter(
            "claude_code_cache_creation_tokens",
            unit="tokens",
            description="Tokens written to prompt cache",
        ),
        "cost_usd": meter.create_counter(
            "claude_code_cost_usd",
            unit="USD",
            description="Estimated cost in USD",
        ),
        "requests": meter.create_counter(
            "claude_code_requests",
            unit="requests",
            description="Number of Claude API calls",
        ),
    }

    state = load_state()
    print(f"[claude-exporter] user={user_label}  endpoint={otlp_url}  interval={args.interval}s")
    print(f"[claude-exporter] watching {claude_dir}/projects/")

    try:
        while True:
            n = poll_once(claude_dir, state, counters, user_label)
            if n:
                print(f"[claude-exporter] processed {n} new records")
            save_state(state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("[claude-exporter] shutting down")
        provider.shutdown()


if __name__ == "__main__":
    main()
