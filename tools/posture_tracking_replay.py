"""Offline R20 posture-clock comparison for existing R18/R19 JSONL logs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from fusion.posture_tracking import (
    PostureTrackingConfig,
    PostureTrackingCoordinator,
    PostureTrackingMode,
)


def replay_rows(rows, *, config=None):
    tracker = PostureTrackingCoordinator(
        config or PostureTrackingConfig(mode=PostureTrackingMode.CLOCK)
    )
    samples = 0
    invalid_pressure = 0
    invalid_ir = 0
    candidate_samples = 0
    deduplicated = 0
    events = []
    cancellations = []
    policy_delta_max = 0.0
    legacy_values = []
    legacy_resets = 0
    previous_legacy = None
    previous_candidate = False
    last = None

    for row in rows:
        sample = sample_from_row(row)
        if sample is None:
            continue
        samples += 1
        result = tracker.observe(**sample)
        if result is None:
            continue
        if not result["sample_valid_pressure"]:
            invalid_pressure += 1
        if not result["sample_valid_ir"]:
            invalid_ir += 1
        if result["candidate_active"]:
            candidate_samples += 1
        if result["canonical_event"] is not None:
            events.append(result["canonical_event"])
        deduplicated += len(result["deduplicated_events"])

        clocks = result["sustained_posture_sec"]
        if clocks is not None:
            policy_delta = (
                clocks["continuous_valid_seated"]
                - clocks["freeze_no_backfill"]
            )
            policy_delta_max = max(policy_delta_max, policy_delta)
            if previous_candidate and not result["candidate_active"] and result["canonical_event"] is None:
                cancellations.append({
                    "timestamp": result["sample_timestamp"],
                    "policy_delta_sec": round(policy_delta, 6),
                })

        legacy = sample["static_hold_sec"]
        if isinstance(legacy, (int, float)) and not isinstance(legacy, bool):
            legacy = float(legacy)
            legacy_values.append(legacy)
            if previous_legacy is not None and legacy + 1e-9 < previous_legacy:
                legacy_resets += 1
            previous_legacy = legacy
        previous_candidate = result["candidate_active"]
        last = result

    return {
        "v": 1,
        "samples": samples,
        "invalid_pressure_samples": invalid_pressure,
        "invalid_ir_samples": invalid_ir,
        "candidate_samples": candidate_samples,
        "candidate_cancellations": cancellations,
        "candidate_policy_finalized": False,
        "max_policy_difference_sec": round(policy_delta_max, 6),
        "canonical_event_count": len(events),
        "canonical_event_kinds": dict(Counter(event["kind"] for event in events)),
        "canonical_events": events,
        "deduplicated_event_count": deduplicated,
        "legacy_static_hold": {
            "samples": len(legacy_values),
            "max_sec": max(legacy_values) if legacy_values else None,
            "final_sec": legacy_values[-1] if legacy_values else None,
            "observed_reset_count": legacy_resets,
        },
        "final_sustained_posture_sec": (
            None if last is None else last["sustained_posture_sec"]
        ),
        "production_behavior_changed": False,
    }


def sample_from_row(row):
    if not isinstance(row, dict):
        return None
    record_type = row.get("record_type")
    if record_type is not None and record_type not in {
        "sample",
        "shadow_sample",
        "posture_sample",
    }:
        return None
    timestamp = row.get("timestamp", row.get("t"))
    pressure = row.get("pressure")
    if pressure is None and all(key in row for key in ("fl", "fr", "bl", "br")):
        pressure = [row["fl"], row["fr"], row["bl"], row["br"]]
    if timestamp is None or pressure is None:
        return None
    seated = row.get("fusion_seated", row.get("seated", True))
    session_id = row.get("session_id")
    if session_id is None:
        session_id = (row.get("shadow") or {}).get("session_id")
    if session_id is None:
        session_id = (row.get("pressure_ir_shadow") or {}).get("session_id")
    static_hold_sec = row.get("static_hold_sec")
    if static_hold_sec is None:
        static_hold_sec = (row.get("shadow") or {}).get("static_hold_sec")
    return {
        "timestamp": timestamp,
        "pressure": pressure,
        "chair_ir_mm": row.get("chair_distance_mm"),
        "seated": bool(seated),
        "session_id": session_id or "offline-replay",
        "static_hold_sec": static_hold_sec,
    }


def read_jsonl(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Compare R20 candidate clock policies using an existing JSONL log"
    )
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    summary = replay_rows(read_jsonl(args.log))
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"[posture-tracking-replay] summary={args.output}")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
