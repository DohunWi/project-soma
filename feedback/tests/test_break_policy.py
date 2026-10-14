"""Hardware-independent tests for logical Feedback and Break Policy."""
import json
import uuid
from pathlib import Path

import pytest

from feedback.config import (
    DEMO_FEEDBACK_CONFIG,
    NORMAL_FEEDBACK_CONFIG,
    feedback_config_for_mode,
)
from feedback.policy.break_policy import FeedbackPolicyState, step

jsonschema = pytest.importorskip("jsonschema")
ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (ROOT / "docs/contracts/feedback_decision.schema.json").read_text(
        encoding="utf-8"
    )
)
VALIDATOR = jsonschema.Draft202012Validator(
    SCHEMA,
    format_checker=jsonschema.FormatChecker(),
)
SESSION_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


def fusion(state="NORMAL", score=100):
    return {"state": state, "score": score}


def advance(
    state,
    decision,
    start,
    seconds,
    *,
    config=DEMO_FEEDBACK_CONFIG,
):
    output = None
    for offset in range(seconds + 1):
        state, output = step(
            state,
            decision,
            start + offset,
            SESSION_ID,
            config=config,
        )
    return state, output


def continue_for(
    state,
    decision,
    start,
    seconds,
    *,
    config=DEMO_FEEDBACK_CONFIG,
):
    """Advance an existing state by exactly ``seconds`` one-second ticks."""
    output = None
    for offset in range(seconds):
        state, output = step(
            state,
            decision,
            start + offset,
            SESSION_ID,
            config=config,
        )
    return state, output


def test_state_to_level_transitions_and_holds():
    state, normal = step(FeedbackPolicyState(), fusion(), 1000.0, SESSION_ID)
    assert normal["level"] == "NORMAL"
    assert normal["transition"] is False

    state, notice = step(state, fusion("CAUTION"), 1001.0, SESSION_ID)
    assert notice["level"] == "NOTICE"
    assert notice["reason"] == "FUSION_CAUTION"
    assert notice["transition"] is True

    state, notice_held = step(state, fusion("CAUTION"), 1002.0, SESSION_ID)
    assert notice_held["level"] == "NOTICE"
    assert notice_held["transition"] is False

    state, warning = step(state, fusion("DANGER"), 1003.0, SESSION_ID)
    assert warning["level"] == "WARNING"
    assert warning["reason"] == "FUSION_DANGER"
    assert warning["transition"] is True

    state, warning_held = step(state, fusion("DANGER"), 1004.0, SESSION_ID)
    assert warning_held["level"] == "WARNING"
    assert warning_held["transition"] is False

    _, recovered = step(state, fusion(), 1005.0, SESSION_ID)
    assert recovered["level"] == "NORMAL"
    assert recovered["transition"] is True


def test_low_score_hold_reaches_break_only_at_demo_boundary():
    state, before = advance(
        FeedbackPolicyState(),
        fusion(score=60),
        1000.0,
        9,
    )
    assert state.low_score_hold_sec == 9.0
    assert before["level"] == "NORMAL"

    state, reached = step(state, fusion(score=60), 1010.0, SESSION_ID)
    assert state.low_score_hold_sec == 10.0
    assert reached["level"] == "BREAK"
    assert reached["reason"] == "LOW_SCORE"
    assert reached["transition"] is True


def test_low_score_hold_resets_above_threshold():
    state, _ = advance(
        FeedbackPolicyState(),
        fusion(score=60),
        1000.0,
        5,
    )
    state, decision = step(state, fusion(score=61), 1006.0, SESSION_ID)

    assert state.low_score_hold_sec == 0.0
    assert decision["level"] == "NORMAL"


def test_danger_hold_reaches_break_and_uses_deterministic_priority():
    state, before = advance(
        FeedbackPolicyState(),
        fusion("DANGER", score=60),
        1000.0,
        9,
    )
    assert before["level"] == "WARNING"

    _, reached = step(state, fusion("DANGER", score=60), 1010.0, SESSION_ID)
    assert reached["level"] == "BREAK"
    assert reached["reason"] == "SUSTAINED_DANGER"


def test_continuous_work_reaches_break():
    state, before = advance(
        FeedbackPolicyState(),
        fusion(),
        1000.0,
        59,
    )
    assert before["level"] == "NORMAL"

    state, reached = step(state, fusion(), 1060.0, SESSION_ID)
    assert state.work_period_sec == 60.0
    assert reached["level"] == "BREAK"
    assert reached["reason"] == "CONTINUOUS_WORK"


def test_break_latches_without_repeating_transition():
    state, _ = advance(
        FeedbackPolicyState(),
        fusion("DANGER"),
        1000.0,
        10,
    )
    state, held = step(state, fusion(), 1011.0, SESSION_ID)

    assert state.break_active is True
    assert held["level"] == "BREAK"
    assert held["reason"] == "SUSTAINED_DANGER"
    assert held["transition"] is False


def test_brief_absence_preserves_break_and_work_period():
    state, _ = advance(
        FeedbackPolicyState(),
        fusion("DANGER"),
        1000.0,
        10,
    )
    work_before = state.work_period_sec
    state, absent = continue_for(state, fusion("ABSENT"), 1011.0, 4)

    assert state.absence_sec == 4.0
    assert state.work_period_sec == work_before
    assert state.break_active is True
    assert state.danger_hold_sec == 0.0
    assert state.low_score_hold_sec == 0.0
    assert absent["level"] == "BREAK"

    state, returned = step(state, fusion(), 1015.0, SESSION_ID)
    assert state.work_period_sec == work_before + 1.0
    assert returned["level"] == "BREAK"


def test_effective_break_clears_latch_resets_work_and_starts_cooldown():
    state, _ = advance(
        FeedbackPolicyState(),
        fusion("DANGER"),
        1000.0,
        10,
    )
    state, decision = continue_for(state, fusion("ABSENT"), 1011.0, 10)

    assert state.absence_sec == 10.0
    assert state.break_active is False
    assert state.work_period_sec == 0.0
    assert state.danger_hold_sec == 0.0
    assert state.low_score_hold_sec == 0.0
    assert state.cooldown_remaining_sec == 30.0
    assert decision["level"] == "NORMAL"
    assert decision["reason"] == "ABSENT"
    assert decision["transition"] is True


def test_effective_absence_resets_work_even_without_active_break():
    state, _ = advance(FeedbackPolicyState(), fusion(), 1000.0, 5)
    assert state.work_period_sec == 5.0

    state, _ = continue_for(state, fusion("ABSENT"), 1006.0, 10)

    assert state.work_period_sec == 0.0
    assert state.cooldown_remaining_sec == 30.0


def test_cooldown_blocks_then_allows_a_new_break():
    state, _ = advance(
        FeedbackPolicyState(),
        fusion("DANGER"),
        1000.0,
        10,
    )
    state, _ = continue_for(state, fusion("ABSENT"), 1011.0, 10)

    state, blocked = continue_for(state, fusion("DANGER"), 1021.0, 28)
    assert state.cooldown_remaining_sec == 2.0
    assert state.danger_hold_sec >= DEMO_FEEDBACK_CONFIG.danger_hold_sec
    assert blocked["level"] == "WARNING"

    state, _ = step(state, fusion("DANGER"), 1049.0, SESSION_ID)
    state, allowed = step(state, fusion("DANGER"), 1050.0, SESSION_ID)
    assert state.cooldown_remaining_sec == 0.0
    assert allowed["level"] == "BREAK"
    assert allowed["reason"] == "SUSTAINED_DANGER"


def test_recommended_break_duration_is_tracked():
    state, _ = advance(FeedbackPolicyState(), fusion(), 1000.0, 5)
    state, _ = continue_for(state, fusion("ABSENT"), 1006.0, 19)
    assert state.recommended_break_reached is False

    state, _ = step(state, fusion("ABSENT"), 1025.0, SESSION_ID)
    assert state.absence_sec == 20.0
    assert state.recommended_break_reached is True


def test_new_policy_state_does_not_reuse_previous_session():
    old, _ = advance(
        FeedbackPolicyState(),
        fusion("DANGER"),
        1000.0,
        10,
    )
    assert old.break_active is True

    new, decision = step(
        FeedbackPolicyState(),
        fusion(),
        2000.0,
        uuid.UUID("22222222-2222-4222-8222-222222222222"),
    )
    assert new.break_active is False
    assert new.work_period_sec == 0.0
    assert decision["level"] == "NORMAL"


def test_first_duplicate_reversed_and_large_gap_do_not_accumulate():
    state, _ = step(FeedbackPolicyState(), fusion("DANGER", 60), 1000.0, SESSION_ID)
    assert state.work_period_sec == 0.0
    assert state.danger_hold_sec == 0.0
    assert state.low_score_hold_sec == 0.0

    state, _ = step(state, fusion("DANGER", 60), 1000.0, SESSION_ID)
    state, _ = step(state, fusion("DANGER", 60), 999.0, SESSION_ID)
    state, _ = step(state, fusion("DANGER", 60), 1010.0, SESSION_ID)
    assert state.work_period_sec == 0.0
    assert state.danger_hold_sec == 0.0
    assert state.low_score_hold_sec == 0.0

    state, _ = step(state, fusion("DANGER", 60), 1011.0, SESSION_ID)
    assert state.work_period_sec == 1.0
    assert state.danger_hold_sec == 1.0
    assert state.low_score_hold_sec == 1.0


def test_absent_never_accumulates_danger_or_low_score():
    state, _ = step(
        FeedbackPolicyState(),
        fusion("ABSENT", score=0),
        1000.0,
        SESSION_ID,
    )
    state, _ = step(state, fusion("ABSENT", score=0), 1001.0, SESSION_ID)

    assert state.danger_hold_sec == 0.0
    assert state.low_score_hold_sec == 0.0
    assert state.work_period_sec == 0.0


def test_normal_profile_danger_boundary():
    state, before = advance(
        FeedbackPolicyState(),
        fusion("DANGER"),
        1000.0,
        59,
        config=NORMAL_FEEDBACK_CONFIG,
    )
    assert before["level"] == "WARNING"

    _, reached = step(
        state,
        fusion("DANGER"),
        1060.0,
        SESSION_ID,
        config=NORMAL_FEEDBACK_CONFIG,
    )
    assert reached["level"] == "BREAK"


def test_profile_selection_uses_soma_mode_names_without_environment_access():
    assert feedback_config_for_mode("demo") is DEMO_FEEDBACK_CONFIG
    assert feedback_config_for_mode("normal") is NORMAL_FEEDBACK_CONFIG
    assert DEMO_FEEDBACK_CONFIG.low_score_hold_sec == 10.0
    assert DEMO_FEEDBACK_CONFIG.danger_hold_sec == 10.0
    assert DEMO_FEEDBACK_CONFIG.continuous_work_sec == 60.0
    assert DEMO_FEEDBACK_CONFIG.effective_break_sec == 10.0
    assert DEMO_FEEDBACK_CONFIG.recommended_break_sec == 20
    assert DEMO_FEEDBACK_CONFIG.break_cooldown_sec == 30.0
    assert NORMAL_FEEDBACK_CONFIG.low_score_hold_sec == 120.0
    assert NORMAL_FEEDBACK_CONFIG.danger_hold_sec == 60.0
    assert NORMAL_FEEDBACK_CONFIG.continuous_work_sec == 3000.0
    assert NORMAL_FEEDBACK_CONFIG.effective_break_sec == 180.0
    assert NORMAL_FEEDBACK_CONFIG.recommended_break_sec == 300
    assert NORMAL_FEEDBACK_CONFIG.break_cooldown_sec == 900.0
    with pytest.raises(ValueError):
        feedback_config_for_mode("invalid")


def test_contract_schema_examples_and_generated_decisions_are_valid():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    for example in SCHEMA["examples"]:
        VALIDATOR.validate(example)

    state, normal = step(FeedbackPolicyState(), fusion(), 1000.0, SESSION_ID)
    VALIDATOR.validate(normal)
    _, notice = step(state, fusion("CAUTION"), 1001.0, SESSION_ID)
    VALIDATOR.validate(notice)

    _, break_decision = advance(
        FeedbackPolicyState(),
        fusion("DANGER"),
        1000.0,
        10,
    )
    VALIDATOR.validate(break_decision)
