"""Offline A/B/C comparison for the R20 Normal static policies.

This tool reads existing diagnostic JSONL.  It never connects to Backend,
hardware, or Supabase and does not alter production Fusion state.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fusion.config import NORMAL_FUSION_TIMING, NORMAL_SOMA_LOAD_CONFIG
from fusion.load import SomaLoadState, update_chair_load
from fusion.posture_tracking import (
    PostureTrackingConfig,
    PostureTrackingCoordinator,
    PostureTrackingMode,
)
from fusion.static_burden import (
    StaticBurdenState,
    static_burden_level,
    update_cumulative_static_burden,
)
from tools.posture_tracking_replay import read_jsonl, sample_from_row


STATIC_EPS = 25.0


@dataclass
class _PolicySummary:
    final_penalty: float = 0.0
    max_penalty: float = 0.0
    final_score: float = 100.0
    caution_first_at: float | None = None
    danger_first_at: float | None = None

    def observe(self, timestamp, penalty, level):
        self.final_penalty = float(penalty)
        self.max_penalty = max(self.max_penalty, float(penalty))
        self.final_score = max(100.0 - float(penalty), 0.0)
        if level >= 1 and self.caution_first_at is None:
            self.caution_first_at = timestamp
        if level >= 2 and self.danger_first_at is None:
            self.danger_first_at = timestamp

    def as_dict(self):
        return {
            "final_static_penalty": round(self.final_penalty, 6),
            "max_static_penalty": round(self.max_penalty, 6),
            "final_static_only_score": round(self.final_score, 6),
            "caution_first_at": self.caution_first_at,
            "danger_first_at": self.danger_first_at,
        }


def compare_rows(rows, *, tracker_config=None):
    """Replay existing rows under legacy, simple-replacement, and C policies."""
    tracker = PostureTrackingCoordinator(
        tracker_config
        or PostureTrackingConfig(mode=PostureTrackingMode.CLOCK)
    )
    legacy_load = SomaLoadState()
    replacement_load = SomaLoadState()
    burden_state = StaticBurdenState()
    burden_penalty = 0.0
    legacy_timer = 0.0
    previous_legacy_timer = 0.0
    replacement_timer = 0.0
    previous_pressure = None
    previous_timestamp = None
    previous_seated = False
    samples = 0
    invalid_rows = 0
    canonical_events = 0
    summaries = {
        "A_legacy": _PolicySummary(),
        "B_simple_replacement": _PolicySummary(),
        "C_cumulative_burden": _PolicySummary(),
    }

    for row in rows:
        sample = sample_from_row(row)
        if sample is None:
            continue
        timestamp = _finite_float(sample["timestamp"])
        pressure = _pressure(sample["pressure"])
        if timestamp is None or pressure is None:
            invalid_rows += 1
            continue
        samples += 1
        dt = _elapsed(previous_timestamp, timestamp)
        seated = bool(sample["seated"])
        if seated and previous_seated and dt > 0:
            activity = (
                math.inf
                if previous_pressure is None
                else sum(abs(a - b) for a, b in zip(pressure, previous_pressure))
            )
            legacy_timer = 0.0 if activity > STATIC_EPS else legacy_timer + dt
        elif not seated:
            legacy_timer = 0.0
        else:
            legacy_timer = 0.0

        if seated and not previous_seated:
            legacy_load = SomaLoadState()
            replacement_load = SomaLoadState()
            burden_penalty = 0.0

        legacy_load = _update_static_only(
            legacy_load,
            previous_static=(
                0.0 if not previous_seated else previous_legacy_timer
            ),
            current_static=legacy_timer,
            seated=seated,
            dt=dt,
        )

        tracked = tracker.observe(
            timestamp,
            pressure,
            sample.get("chair_ir_mm"),
            seated=seated,
            session_id=sample.get("session_id"),
            static_hold_sec=legacy_timer,
        )
        clocks = tracked["sustained_posture_sec"] or {
            "continuous_valid_seated": replacement_timer
        }
        next_replacement = float(clocks["continuous_valid_seated"])
        replacement_load = _update_static_only(
            replacement_load,
            previous_static=replacement_timer,
            current_static=next_replacement,
            seated=seated,
            dt=dt,
        )
        replacement_timer = next_replacement

        burden_state, burden_penalty, _diagnostic = update_cumulative_static_burden(
            burden_state,
            previous_penalty=burden_penalty,
            legacy_penalty=legacy_load.static_penalty,
            posture_tracking=tracked,
            previous_seated=previous_seated,
            seated=seated,
            load_config=NORMAL_SOMA_LOAD_CONFIG,
        )
        if tracked["canonical_event"] is not None:
            canonical_events += 1

        summaries["A_legacy"].observe(
            timestamp,
            legacy_load.static_penalty,
            _timer_level(legacy_timer),
        )
        summaries["B_simple_replacement"].observe(
            timestamp,
            replacement_load.static_penalty,
            _timer_level(replacement_timer),
        )
        summaries["C_cumulative_burden"].observe(
            timestamp,
            burden_penalty,
            static_burden_level(burden_penalty),
        )
        previous_legacy_timer = legacy_timer
        previous_pressure = pressure
        previous_timestamp = timestamp
        previous_seated = seated

    return {
        "v": 1,
        "samples": samples,
        "invalid_rows": invalid_rows,
        "canonical_event_count": canonical_events,
        "candidate_time_policy": "continuous_valid_seated",
        "policies": {name: summary.as_dict() for name, summary in summaries.items()},
        "final_clocks": {
            "legacy_static_hold_sec": round(legacy_timer, 6),
            "simple_replacement_sec": round(replacement_timer, 6),
            "posture_episode_sec": round(burden_state.posture_episode_sec, 6),
            "static_eligible_total_sec": round(
                burden_state.static_eligible_total_sec,
                6,
            ),
        },
        "production_behavior_changed": False,
    }


def _update_static_only(load, *, previous_static, current_static, seated, dt):
    updated = update_chair_load(
        load,
        previous_static_sec=previous_static,
        static_sec=current_static,
        previous_imbalance_sec=0.0,
        imbalance_sec=0.0,
        balance="CENTER",
        seated=seated,
        dt=dt,
        config=NORMAL_SOMA_LOAD_CONFIG,
    )
    return SomaLoadState(
        static_penalty=updated.static_penalty,
        absent_sec=updated.absent_sec,
    )


def _timer_level(seconds):
    if seconds + 1e-9 >= NORMAL_FUSION_TIMING.static_danger_sec:
        return 2
    if seconds + 1e-9 >= NORMAL_FUSION_TIMING.static_caution_sec:
        return 1
    return 0


def _elapsed(previous, current):
    if previous is None:
        return 0.0
    delta = current - previous
    if delta <= 0 or delta > NORMAL_FUSION_TIMING.max_gap_sec:
        return 0.0
    return delta


def _finite_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _pressure(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    converted = [_finite_float(item) for item in value]
    return converted if all(item is not None for item in converted) else None


def build_parser():
    parser = argparse.ArgumentParser(
        description="Offline R20 Normal static-policy A/B/C comparison"
    )
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    summary = compare_rows(read_jsonl(args.log))
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"[static-policy-compare] summary={args.output}")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
