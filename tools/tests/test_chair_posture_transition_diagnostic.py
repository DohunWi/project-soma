import io
import json

from tools.chair_posture_transition_diagnostic import (
    STAGES,
    TransitionRecorder,
    analyze_rows,
    run_continuous,
    write_summary,
)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def observation(pressure, *, t=1000.0):
    return {
        "chair": {
            "v": 1,
            "t": t,
            "source": "chair",
            "device_id": "chair-test",
            "user_name": "guest",
            "chair": {"pressure": pressure, "ir": [250]},
        },
        "state": {
            "state": "NORMAL",
            "score": 100,
            "metrics": {"seated": True, "static_hold_sec": 5.0},
        },
    }


def rows(sink, record_type=None):
    result = [json.loads(line) for line in sink.getvalue().splitlines()]
    if record_type is None:
        return result
    return [row for row in result if row.get("record_type") == record_type]


def emit(recorder, clock, pressure):
    clock.value += 1
    return recorder.record(observation(pressure, t=1000 + clock.value))


def test_real_backend_nested_observation_records_requested_fields():
    sink = io.StringIO()
    clock = Clock()
    recorder = TransitionRecorder(sink, monotonic=clock)
    recorder.begin("CENTER", "center")
    row = emit(recorder, clock, [300, 100, 200, 400])
    assert row["timestamp"] == 1001.0
    assert row["stage"] == "CENTER"
    assert row["experiment_elapsed_sec"] == 1
    assert row["valid_pressure"] is True
    assert row["valid_distribution"] is True
    assert row["channel_share"]["fl"] == 0.3
    assert row["fusion_state"] == "NORMAL"
    assert row["fusion_seated"] is True
    assert row["static_hold_sec"] == 5.0


def test_activity_remains_continuous_across_stage_boundary():
    sink = io.StringIO()
    clock = Clock()
    recorder = TransitionRecorder(sink, monotonic=clock)
    recorder.begin("CENTER", "center")
    first = emit(recorder, clock, [100, 100, 100, 100])
    recorder.transition("LEFT", "move left")
    second = emit(recorder, clock, [200, 100, 200, 100])
    assert first["activity"] is None
    assert second["activity"] == 200


def test_transition_events_are_separate_and_credential_safe():
    sink = io.StringIO()
    clock = Clock()
    recorder = TransitionRecorder(sink, monotonic=clock)
    recorder.begin("CENTER", "center")
    clock.value = 30
    recorder.transition("LEFT", "move left")
    events = rows(sink, "stage_transition")
    assert events == [
        {
            "record_type": "stage_transition",
            "recorded_at": events[0]["recorded_at"],
            "from_stage": None,
            "to_stage": "CENTER",
            "transition_instruction": "center",
            "experiment_elapsed_sec": 0.0,
        },
        {
            "record_type": "stage_transition",
            "recorded_at": events[1]["recorded_at"],
            "from_stage": "CENTER",
            "to_stage": "LEFT",
            "transition_instruction": "move left",
            "experiment_elapsed_sec": 30,
        },
    ]
    serialized = json.dumps(events)
    assert "token" not in serialized.lower()
    assert "authorization" not in serialized.lower()


def test_invalid_sensor_data_is_preserved_without_losing_stage():
    sink = io.StringIO()
    clock = Clock()
    recorder = TransitionRecorder(sink, monotonic=clock)
    recorder.begin("CENTER", "center")
    row = emit(recorder, clock, [1, 2, 3])
    assert row["stage"] == "CENTER"
    assert row["valid_pressure"] is False
    assert row["invalid_reason"] == "pressure_must_have_four_finite_numbers"
    assert row["observed_shape"]["pressure_length"] == 3


def test_summary_contains_timeline_spikes_returns_and_stabilization(tmp_path):
    sink = io.StringIO()
    clock = Clock()
    recorder = TransitionRecorder(sink, monotonic=clock)
    stage_pressures = {
        "CENTER": [[250, 250, 250, 250]] * 6,
        "LEFT": [[250, 250, 250, 250], [400, 100, 400, 100]] * 3,
        "CENTER_RETURN_1": [[400, 100, 400, 100], [250, 250, 250, 250]] * 3,
        "RIGHT": [[250, 250, 250, 250], [100, 400, 100, 400]] * 3,
        "CENTER_RETURN_2": [[100, 400, 100, 400], [250, 250, 250, 250]] * 3,
    }
    first, instruction = STAGES[0]
    recorder.begin(first, instruction)
    for stage_index, (label, instruction) in enumerate(STAGES):
        if stage_index:
            recorder.transition(label, instruction)
        for pressure in stage_pressures[label]:
            emit(recorder, clock, pressure)
    summary = analyze_rows(rows(sink))
    assert summary["classification_thresholds_selected"] is False
    assert len(summary["transition_events"]) == 5
    assert len(summary["distance_timeline"]) == 30
    assert summary["stages"]["CENTER"]["valid_samples"] == 6
    assert summary["stages"]["LEFT"]["distance_from_initial_center"]["max"] > 0
    assert summary["center_return_distance"]["CENTER_RETURN_1"]["median"] > 0
    assert summary["activity_spikes_over_existing_static_eps"]
    assert "method" in summary["technical_stabilization_estimates"]["LEFT"]
    summary_path = write_summary(tmp_path / "transition.jsonl", summary)
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary


def test_first_enter_then_all_stages_run_without_additional_input():
    sink = io.StringIO()
    clock = Clock()
    recorder = TransitionRecorder(sink, monotonic=clock)
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        return ""

    def sleep(_seconds):
        recorder.record(observation([250, 250, 250, 250]))
        clock.value += 1

    completed = run_continuous(
        recorder,
        record_sec=1,
        read=read,
        sleep=sleep,
        write=lambda _line: None,
    )
    assert len(prompts) == 1
    assert completed == [label for label, _ in STAGES]
    assert [row["stage"] for row in rows(sink, "sample")] == completed
    assert len(rows(sink, "stage_transition")) == 5
    assert recorder.stage is None
