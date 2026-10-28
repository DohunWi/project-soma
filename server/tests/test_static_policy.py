"""Normal-only integration coverage for the cumulative static policy."""

import pytest

from fusion.blink_load import BlinkPolicyMode
from fusion.posture_tracking import PostureTrackingConfig, PostureTrackingMode
from fusion.static_burden import StaticPolicyMode
from server.app import ChairPipeline, create_app
from server.config import DEMO_PROFILE, NORMAL_PROFILE


CENTER = [900, 900, 900, 900]
LEFT = [1000, 500, 1000, 500]


def payload(timestamp, pressure=CENTER, *, ir=50):
    return {
        "v": 1,
        "t": float(timestamp),
        "source": "chair",
        "device_id": "static-v2-test",
        "user_name": "test",
        "chair": {"pressure": list(pressure), "ir": [ir]},
    }


def pipeline(*, static_policy=StaticPolicyMode.CUMULATIVE_V2, tracking=None):
    tracker_config = tracking or PostureTrackingConfig(
        mode=PostureTrackingMode.CLOCK
    )
    result = ChairPipeline(
        NORMAL_PROFILE.fusion,
        NORMAL_PROFILE.load,
        distance_timing=NORMAL_PROFILE.distance_evidence_timing,
        posture_tracking_config=tracker_config,
        static_policy_mode=static_policy,
    )
    result.reset("static-v2-session")
    return result


def tick(subject, timestamp, pressure=CENTER, *, ir=50):
    item = payload(timestamp, pressure, ir=ir)
    sample = subject.prepare_validated(item)
    return subject.process_prepared(item, sample)


@pytest.mark.parametrize(
    ("seconds", "expected_penalty", "expected_score", "expected_state"),
    [
        (1200, 10.0, 90, "CAUTION"),
        (2700, 20.0, 80, "DANGER"),
        (3600, 25.0, 75, "DANGER"),
    ],
)
def test_synthetic_20_45_60_minute_continuous_pose(
    seconds,
    expected_penalty,
    expected_score,
    expected_state,
):
    subject = pipeline()
    decision = None
    for timestamp in range(seconds + 1):
        decision = tick(subject, timestamp)

    state = subject.debug_state()
    diagnostic = subject.latest_static_burden_diagnostic
    assert state.load.static_penalty == pytest.approx(expected_penalty)
    assert decision["score"] == expected_score
    assert decision["state"] == expected_state
    assert decision["reasons"] == ["static_hold"]
    assert diagnostic["static_eligible_total_sec"] == pytest.approx(
        max(seconds - 300, 0)
    )


def test_confirmed_change_resets_episode_but_never_recovers_static_penalty():
    tracking = PostureTrackingConfig(
        mode=PostureTrackingMode.CLOCK,
        anchor_samples=3,
        pressure_candidate_distance=0.1,
        pressure_candidate_sec=2.0,
        pressure_candidate_radius=0.02,
    )
    subject = pipeline(tracking=tracking)
    for timestamp in range(601):
        tick(subject, timestamp)
    before = subject.debug_state().load.static_penalty
    assert before == pytest.approx(5.0)

    canonical = None
    for timestamp in range(601, 612):
        tick(subject, timestamp, LEFT, ir=-1)
        diagnostic = subject.latest_posture_diagnostic
        if diagnostic and diagnostic["canonical_event"] is not None:
            canonical = diagnostic["canonical_event"]
            break

    assert canonical is not None
    assert canonical["kind"] == "PRESSURE_CHANGE"
    assert subject.debug_state().static_burden.posture_episode_sec == 0.0
    # The valid seconds before confirmation still belong to the previous
    # episode, so they are charged once before that episode is reset.
    assert subject.debug_state().static_burden.static_eligible_total_sec > 300.0
    after_change = subject.debug_state().load.static_penalty
    assert after_change >= before

    # A new episode stays inside its own grace window; seated posture change
    # cannot pay down the burden already accumulated by the earlier episode.
    event_time = int(canonical["confirmed_at"])
    for timestamp in range(event_time + 1, event_time + 121):
        tick(subject, timestamp, LEFT, ir=-1)
    assert subject.debug_state().load.static_penalty == pytest.approx(after_change)


def test_default_legacy_pipeline_does_not_create_static_v2_diagnostics():
    subject = pipeline(
        static_policy=StaticPolicyMode.LEGACY,
        tracking=PostureTrackingConfig(mode=PostureTrackingMode.OFF),
    )
    decision = tick(subject, 0)
    assert decision["score"] == 100
    assert subject.latest_posture_diagnostic is None
    assert subject.latest_static_burden_diagnostic is None


def test_static_v2_preserves_absence_and_new_seated_interval_lifecycle():
    subject = pipeline()
    for timestamp in range(1201):
        decision = tick(subject, timestamp)
    assert decision["state"] == "CAUTION"
    assert decision["score"] == 90

    for timestamp in range(1201, 1206):
        decision = tick(subject, timestamp, [0, 0, 0, 0], ir=-1)
    assert decision["state"] == "ABSENT"
    assert subject.debug_state().static_burden.static_eligible_total_sec == 0.0

    decision = tick(subject, 1206, CENTER)
    assert decision["state"] == "NORMAL"
    assert decision["score"] == 100
    assert subject.debug_state().static_burden.static_eligible_total_sec == 0.0


@pytest.mark.parametrize(
    ("profile", "static_policy", "blink_policy", "posture_tracking"),
    [
        (NORMAL_PROFILE, "cumulative-v2", "score-v1", "clock"),
        (DEMO_PROFILE, "legacy", "legacy", "off"),
    ],
)
def test_profile_defaults_select_normal_v2_and_demo_legacy(
    monkeypatch,
    profile,
    static_policy,
    blink_policy,
    posture_tracking,
):
    for name in (
        "SOMA_STATIC_POLICY",
        "SOMA_BLINK_POLICY",
        "SOMA_POSTURE_TRACKING_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    app, _socketio = create_app(testing=True, runtime_profile=profile)

    assert app.extensions["static_policy_mode"] == static_policy
    assert app.extensions["blink_policy_mode"] == blink_policy
    assert app.extensions["posture_tracking_mode"] == posture_tracking


def test_normal_environment_can_restore_both_legacy_policies(monkeypatch):
    monkeypatch.setenv("SOMA_STATIC_POLICY", "legacy")
    monkeypatch.setenv("SOMA_BLINK_POLICY", "legacy")

    app, _socketio = create_app(testing=True, runtime_profile=NORMAL_PROFILE)

    assert app.extensions["static_policy_mode"] == "legacy"
    assert app.extensions["blink_policy_mode"] == "legacy"
    assert app.extensions["posture_tracking_mode"] == "off"


def test_explicit_policy_arguments_override_environment(monkeypatch):
    monkeypatch.setenv("SOMA_STATIC_POLICY", "legacy")
    monkeypatch.setenv("SOMA_BLINK_POLICY", "legacy")

    app, _socketio = create_app(
        testing=True,
        runtime_profile=NORMAL_PROFILE,
        static_policy_mode="cumulative-v2",
        blink_policy_mode="score-v1",
    )

    assert app.extensions["static_policy_mode"] == "cumulative-v2"
    assert app.extensions["blink_policy_mode"] == "score-v1"
    assert app.extensions["posture_tracking_mode"] == "clock"


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"SOMA_STATIC_POLICY": "cumulative-v2"}, "Normal-only"),
        ({"SOMA_BLINK_POLICY": "score-v1"}, "Normal-only"),
    ],
)
def test_demo_rejects_normal_only_policy_overrides(monkeypatch, environment, message):
    monkeypatch.delenv("SOMA_STATIC_POLICY", raising=False)
    monkeypatch.delenv("SOMA_BLINK_POLICY", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        create_app(testing=True, runtime_profile=DEMO_PROFILE)
