"""Explicit Chair-only mode without changing All-source or downstream behavior."""
import pytest

from fusion.calibration import CalibrationAccumulator
from fusion.distance_evidence import DistanceEvidenceClass
from fusion.state import OCCUPANCY_MIN
from server.config import DEMO_PROFILE, NORMAL_PROFILE, calibration_for_source_mode
from server.measurement_calibration import MeasurementCalibration, MeasurementPhase
from server.app import MEASUREMENT_PHASE_VALIDATOR, STATE_VALIDATOR
from server.tests.test_measurement_calibration import (
    USER_ID, USER_TOKEN, SENSOR_TOKEN, make_runtime, start, named,
    chair_payload, vision_payload,
)


@pytest.mark.parametrize('profile,count,duration', [(DEMO_PROFILE, 5, 5), (NORMAL_PROFILE, 8, 8)])
def test_chair_only_exact_count_duration_then_measuring_and_chair_state(profile, count, duration):
    app, socket, clock, timers, persistence = make_runtime(profile, source_mode='chair-only')
    owner = socket.test_client(app, auth={'token': USER_TOKEN})
    producer = socket.test_client(app, auth={'token': SENSOR_TOKEN})
    assert start(app).status_code == 201
    phases = named(owner, 'measurement_phase')
    assert phases[-1]['calibration']['required_samples'] == {'vision': 0, 'chair_ir': count}
    for i in range(count-1):
        clock.set(i)
        producer.emit('sensor_data', chair_payload(i))
    clock.set(duration - .5)
    producer.emit('sensor_data', chair_payload(duration - .5, ir=-1))
    clock.set(duration)
    assert not app.extensions['measurement_calibration'].evaluate(duration).became_ready
    assert app.extensions['measurement_calibration'].phase is MeasurementPhase.CALIBRATING
    assert not persistence.calls
    producer.emit('sensor_data', chair_payload(duration))
    phases += named(owner, 'measurement_phase')
    assert [event['phase'] for event in phases[-2:]] == ['READY', 'MEASURING']
    assert all(MEASUREMENT_PHASE_VALIDATOR.is_valid(event) for event in phases)
    accepted = phases[-1]['calibration']
    assert accepted['progress'] == 1
    assert accepted['valid_samples'] == {'vision': 0, 'chair_ir': count}
    assert accepted['readiness'] == {'duration': True, 'vision': True, 'chair_ir': True}
    calibration = app.extensions['measurement_calibration']
    assert calibration.baselines.face_working_baseline_cm is None
    assert calibration.baselines.face_mad_cm is None
    assert calibration.baselines.chair_ir_baseline_mm == 250
    assert timers[0].cancelled
    clock.set(duration + 1)
    producer.emit('sensor_data', chair_payload(duration + 1))
    events = owner.get_received()
    states = [event['args'][0] for event in events if event['name'] == 'state']
    assert len(states) == 1 and STATE_VALIDATOR.is_valid(states[0])
    assert states[0]['state'] == 'NORMAL'
    assert states[0]['confidence'] == pytest.approx(.45)
    assert 'face_distance_cm' not in states[0]['metrics']
    assert any(event['name'] == 'feedback' for event in events)
    assert len(persistence.calls) == 1
    assert persistence.calls[-1][2]['user_id'] == USER_ID
    assert calibration.latest_distance_evidence.classification is DistanceEvidenceClass.UNKNOWN
    app.test_client().post('/api/measurement/stop', headers={'Authorization': 'Bearer ' + USER_TOKEN})
    owner.get_received()
    producer.emit('sensor_data', chair_payload(duration + 2))
    assert not named(owner, 'state') and len(persistence.calls) == 1
    assert calibration.baselines is None
    owner.disconnect()
    producer.disconnect()


def test_five_valid_samples_before_five_seconds_cannot_finish():
    config = calibration_for_source_mode(DEMO_PROFILE, 'chair-only')
    calibration = MeasurementCalibration(config, occupancy_min=OCCUPANCY_MIN)
    calibration.start('test-session', 0)
    for i in range(5):
        calibration.observe({'pressure': [300]*4, 'ir': [250]}, i)
    assert calibration.phase is MeasurementPhase.CALIBRATING
    progress = calibration.snapshot_for(4, 1004)['calibration']
    assert progress['progress'] == .8
    assert progress['valid_samples']['vision'] == progress['required_samples']['vision'] == 0
    assert calibration.evaluate(5).became_ready


@pytest.mark.parametrize('profile,count', [(DEMO_PROFILE, 5), (NORMAL_PROFILE, 8)])
@pytest.mark.parametrize('vision_kind', ['missing', 'stale'])
def test_all_source_does_not_auto_downgrade_without_fresh_vision(profile, count, vision_kind):
    app, socket, clock, timers, persistence = make_runtime(profile)
    owner = socket.test_client(app, auth={'token': USER_TOKEN})
    producer = socket.test_client(app, auth={'token': SENSOR_TOKEN})
    start(app)
    named(owner, 'measurement_phase')
    if vision_kind == 'stale':
        producer.emit('sensor_data', vision_payload(0))
    for i in range(4, count + 5):
        clock.set(i)
        producer.emit('sensor_data', chair_payload(i))
    calibration = app.extensions['measurement_calibration']
    assert app.extensions['source_mode'] == 'all'
    assert calibration.phase is MeasurementPhase.CALIBRATING
    progress = named(owner, 'measurement_phase')[-1]['calibration']
    assert progress['required_samples']['vision'] == count
    assert progress['valid_samples']['vision'] == 0 and progress['progress'] == 0
    assert not persistence.calls and not named(owner, 'state')
    clock.set(profile.calibration.timeout_sec)
    timers[0].fire()
    assert named(owner, 'measurement_phase')[-1]['calibration']['status'] == 'FAILED'
    owner.disconnect()
    producer.disconnect()


@pytest.mark.parametrize('profile,count', [(DEMO_PROFILE, 5), (NORMAL_PROFILE, 8)])
def test_all_source_still_requires_all_vision_samples(profile, count):
    app, socket, clock, _, _ = make_runtime(profile)
    producer = socket.test_client(app, auth={'token': SENSOR_TOKEN})
    start(app)
    for i in range(count - 1):
        clock.set(i)
        producer.emit('sensor_data', vision_payload(i))
        producer.emit('sensor_data', chair_payload(i))
    clock.set(count)
    producer.emit('sensor_data', chair_payload(count - .5))
    calibration = app.extensions['measurement_calibration']
    assert calibration.phase is MeasurementPhase.CALIBRATING
    producer.emit('sensor_data', vision_payload(count))
    producer.emit('sensor_data', chair_payload(count))
    assert calibration.phase is MeasurementPhase.MEASURING
    assert calibration.baselines.face_working_baseline_cm == 60
    producer.disconnect()


def test_chair_only_ignores_incidental_vision_for_calibration_and_handles_null_face():
    config = calibration_for_source_mode(DEMO_PROFILE, 'chair-only')
    calibration = MeasurementCalibration(config, occupancy_min=OCCUPANCY_MIN)
    calibration.start('test-session', 0)
    for i in range(6):
        calibration.observe({'pressure': [300]*4, 'ir': [250], 'face_detected': True,
                             'face_distance_cm': 1000 if i % 2 else 10,
                             '_vision_sender_t': i}, i)
    assert calibration.accept_ready()
    assert calibration.snapshot_for(6, 1006)['calibration']['valid_samples']['vision'] == 0
    evidence = calibration.relative_evidence({'pressure': [300]*4, 'ir': [290],
                                              'face_detected': True, 'face_distance_cm': 40}, now=6)
    assert evidence == {'backrest_departure_delta_mm': 40}
    assert not calibration.latest_distance_evidence.vision_available
    assert calibration.latest_distance_evidence.classification is DistanceEvidenceClass.UNKNOWN


def test_chair_only_still_rejects_unstable_ir_and_absent_samples():
    config = calibration_for_source_mode(DEMO_PROFILE, 'chair-only')
    accumulator = CalibrationAccumulator()
    for i in range(6):
        accumulator.observe({'pressure': [300]*4, 'ir': [100 if i % 2 else 400]},
                            occupancy_min=OCCUPANCY_MIN, collect_vision=False)
    assert not accumulator.assess(config).ready
    empty = CalibrationAccumulator()
    empty.observe({'pressure': [1]*4, 'ir': [250]}, occupancy_min=OCCUPANCY_MIN,
                  collect_vision=False)
    assert not empty.assess(config).ready
