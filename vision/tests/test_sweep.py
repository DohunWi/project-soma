"""Tests for reaction-aware, evaluation-only EAR threshold sweeping."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "vision_eval_sweep",
    ROOT / "vision" / "eval" / "sweep.py",
)
sweep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sweep
SPEC.loader.exec_module(sweep)


def frame(t, ear, label="OPEN"):
    return sweep.Frame(t, ear, ear, ear, label, True, True)


def recording(frames, cues=(1.0,), condition="test"):
    return sweep.Recording(
        Path(f"{condition}.jsonl"),
        {"condition": condition},
        tuple(frames),
        tuple(cues),
    )


def test_load_recording_supports_new_fields_and_legacy_ear_alias(tmp_path):
    path = tmp_path / "recording.jsonl"
    rows = [
        {"type": "meta", "condition": "light-off"},
        {
            "type": "frame",
            "t": 1.0,
            "left_ear": 0.20,
            "right_ear": 0.22,
            "combined_ear": 0.21,
            "ear": 0.99,
            "label": "OPEN",
            "face_detected": True,
            "frontal": True,
        },
        {"type": "frame", "t": 1.1, "ear": 0.19, "label": "BLINK"},
        {"type": "cue", "t": 1.05},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    loaded = sweep.load_recording(path)

    assert loaded.condition == "light-off"
    assert loaded.frames[0].combined_ear == pytest.approx(0.21)
    assert loaded.frames[1].combined_ear == pytest.approx(0.19)
    assert loaded.cues == (1.05,)


def test_open_baseline_excludes_cue_response_and_label_transition_frames():
    frames = [
        frame(0.0, 0.30),
        frame(0.5, 0.31),
        frame(0.95, 0.08),  # cue response contamination despite OPEN prompt
        frame(1.0, 0.07, "BLINK"),
        frame(1.5, 0.30, "BLINK"),
        frame(1.6, 0.12),  # immediately after label transition
        frame(2.0, 0.32),
        frame(2.5, 0.31),
    ]
    sample = recording(frames)

    clean = sweep.usable_open_frames(
        sample,
        transition_margin_sec=0.15,
        match_early_sec=0.10,
        match_late_sec=0.70,
    )

    assert [item.t for item in clean] == [0.0, 0.5, 2.0, 2.5]
    baselines = sweep.baseline_values(clean)
    assert baselines["median"] == pytest.approx(0.31)
    assert baselines["p75"] == pytest.approx(0.3125)
    assert baselines["trimmed_mean"] == pytest.approx(0.31)


def test_matching_is_one_to_one_and_uses_asymmetric_reaction_window():
    events = (
        sweep.BlinkEvent(0.70, 0.90, 200),  # too early for cue 1.0
        sweep.BlinkEvent(1.30, 1.50, 200),
        sweep.BlinkEvent(1.45, 1.65, 200),  # unmatched duplicate
        sweep.BlinkEvent(3.40, 3.60, 200),
    )

    assert sweep.match_events((1.0, 3.0), events, 0.05, 0.70) == (2, 2, 0)


def test_absolute_threshold_can_remain_stuck_when_open_ear_never_exceeds_open():
    sample = recording([
        frame(0.0, 0.20),
        frame(0.1, 0.03, "BLINK"),
        frame(0.3, 0.04, "BLINK"),
        frame(0.5, 0.22),
        frame(2.0, 0.23),
    ])

    result = sweep.evaluate(sample, 0.21, 0.25)

    assert result.tp == 0
    assert result.fn == 1
    assert result.candidate_stuck_at_end is True
    assert result.longest_candidate_ms == pytest.approx(2000)


def test_closed_hold_event_is_reported_separately_from_cue_metrics():
    sample = recording([
        frame(0.0, 0.30),
        frame(1.2, 0.10, "BLINK"),
        frame(1.4, 0.30, "BLINK"),
        frame(2.0, 0.30),
        frame(3.0, 0.10, "CLOSED_HOLD"),
        frame(3.2, 0.10, "CLOSED_HOLD"),
        frame(3.4, 0.30, "CLOSED_HOLD"),
        frame(4.0, 0.30),
    ])

    result = sweep.evaluate(sample, 0.20, 0.25, transition_margin_sec=0.05)

    assert (result.tp, result.fp, result.fn) == (1, 1, 0)
    assert result.hold_false_events == 1
    assert result.candidate_stuck_at_end is False


def test_relative_sweep_uses_source_baseline_for_cross_condition_target():
    source = recording([
        frame(0.0, 0.30),
        frame(0.5, 0.32),
        frame(2.0, 0.31),
        frame(2.5, 0.30),
    ], cues=(), condition="light-off")
    target = recording([
        frame(0.0, 0.30),
        frame(1.2, 0.05, "BLINK"),
        frame(1.4, 0.30, "BLINK"),
        frame(2.0, 0.30),
    ], condition="light-on")

    rows = sweep.relative_sweep(
        source,
        target,
        closed_ratios=(0.50,),
        open_ratios=(0.80,),
        transition_margin_sec=0.0,
    )

    assert {row.baseline_method for row in rows} == {
        "median", "p75", "trimmed_mean"
    }
    assert all(row.source == "light-off" for row in rows)
    assert all(row.target == "light-on" for row in rows)
    assert all(row.result.tp == 1 for row in rows)
    median_row = next(row for row in rows if row.baseline_method == "median")
    assert median_row.baseline == pytest.approx(0.305)
    assert median_row.closed_threshold == pytest.approx(0.1525)


def test_production_threshold_constants_remain_the_evaluated_reference():
    assert sweep.ABSOLUTE_CLOSED == 0.21
    assert sweep.ABSOLUTE_OPEN == 0.25
    assert sweep.MIN_CLOSED_MS == 60
    assert sweep.MAX_CLOSED_MS == 500
