"""Recorded-session ownership, DB isolation and Stop/restart integration tests."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.app import HISTORY_SESSIONS_VALIDATOR, STATE_HISTORY_VALIDATOR
from server.state_history import (
    HISTORY_SQL, SESSION_LIST_SQL, SESSION_SQL, HistorySessionNotFound,
    HistoryUnavailable, StateHistoryReader, parse_session_id,
)
from server.state_persistence import StatePersistence
from server.tests.test_app import (
    USER_ID, OTHER_USER_ID, USER_TOKEN, OTHER_TOKEN, SENSOR_TOKEN,
    make_app, chair_payload,
)
from server.tests.test_state_history import FakeConnection, FakeCursor

SID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OLDER_SID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
NOW = datetime(2026, 10, 14, 3, tzinfo=timezone.utc)


class SnapshotStore:
    """DB double executing only the production reader's parameterized queries."""

    def __init__(self):
        self.rows = []
        self.executions = []
        self.error = None

    def put_state_log(self, row):
        self.rows.append(dict(row))
        return True

    def connect(self):
        return FakeConnection(StoreCursor(self))


class StoreCursor(FakeCursor):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def execute(self, sql, params):
        self.store.executions.append((sql, params))
        if self.store.error:
            raise self.store.error
        owned = [row for row in self.store.rows if row['user_id'] == params[0]]
        if sql in (SESSION_LIST_SQL, SESSION_SQL):
            groups = {}
            for row in owned:
                sid = row['session_id']
                if sid is not None and (sql == SESSION_LIST_SQL or sid == params[1]):
                    groups.setdefault(sid, []).append(row['measured_at'])
            self.rows = sorted([(sid, min(times), max(times), len(times))
                                for sid, times in groups.items()],
                               key=lambda row: (row[2], row[0]), reverse=True)
        else:
            assert sql == HISTORY_SQL
            assert params[0:2] == params[3:5]
            owned = sorted([row for row in owned if row['session_id'] == params[1]],
                           key=lambda row: row['measured_at'])
            before = [row for row in owned if row['measured_at'] < params[2]]
            ranged = [row for row in owned if params[5] <= row['measured_at'] <= params[6]]
            self.rows = [snapshot_row(row, True) for row in before[-1:]] + [
                snapshot_row(row, False) for row in ranged]


def snapshot_row(row, baseline):
    return tuple(row.get(key) for key in (
        'measured_at', 'state', 'score', 'confidence', 'reasons', 'balance',
        'static_hold_sec', 'low_blink_sec', 'session_sec', 'chair_distance_mm',
        'blink_rate', 'face_distance_cm',
    )) + (baseline,)


def stored_row(user_id=USER_ID, sid=SID, time=NOW, score=95):
    return dict(user_id=str(user_id), session_id=str(sid) if sid else None,
                measured_at=time, state='NORMAL', score=score, confidence=0.45, reasons=[])


def setup_reader():
    store = SnapshotStore()
    store.rows = [stored_row(time=NOW - timedelta(seconds=5)), stored_row(),
                  stored_row(sid=OLDER_SID, time=NOW - timedelta(days=1)),
                  stored_row(OTHER_USER_ID, time=NOW + timedelta(days=1)),
                  stored_row(sid=None)]
    reader = StateHistoryReader(connection_factory=store.connect)
    app, socket = make_app(history_reader=reader)
    return store, reader, app, socket


def get(client, path, token=USER_TOKEN, **kwargs):
    return client.get(path, headers={'Authorization': 'Bearer ' + token}, **kwargs)


@pytest.mark.parametrize('path', ['/api/state/history/sessions', f'/api/state/history/sessions/{SID}'])
@pytest.mark.parametrize('token', [None, 'invalid-token'])
def test_recorded_history_requires_auth(path, token):
    store, _, app, _ = setup_reader()
    response = app.test_client().get(path, headers={} if token is None else
                                     {'Authorization': 'Bearer ' + token})
    assert response.status_code == 401
    assert not store.executions


def test_list_is_user_scoped_latest_first_and_excludes_legacy():
    store, _, app, _ = setup_reader()
    payload = get(app.test_client(), '/api/state/history/sessions').get_json()
    HISTORY_SESSIONS_VALIDATOR.validate(payload)
    assert payload['user_id'] == str(USER_ID)
    assert [row['session_id'] for row in payload['sessions']] == [str(SID), str(OLDER_SID)]
    assert payload['sessions'][0]['snapshot_count'] == 2
    assert 'started_at' not in payload['sessions'][0]
    assert store.executions[-1] == (SESSION_LIST_SQL, (str(USER_ID),))
    other = get(app.test_client(), '/api/state/history/sessions', OTHER_TOKEN).get_json()
    assert len(other['sessions']) == 1
    assert other['sessions'][0]['last_measured_at'] != payload['sessions'][0]['last_measured_at']


def test_missing_and_other_owner_return_identical_404_without_fetch():
    store, _, app, _ = setup_reader()
    # OLD SID belongs exclusively to A; B cannot discover its existence.
    missing = get(app.test_client(), f'/api/state/history/sessions/{uuid.uuid4()}', OTHER_TOKEN)
    foreign = get(app.test_client(), f'/api/state/history/sessions/{OLDER_SID}', OTHER_TOKEN)
    assert missing.status_code == foreign.status_code == 404
    assert missing.get_json() == foreign.get_json()
    assert all(sql == SESSION_SQL for sql, _ in store.executions)


def test_even_shared_session_uuid_never_crosses_user_boundary():
    _, _, app, _ = setup_reader()
    a = get(app.test_client(), f'/api/state/history/sessions/{SID}').get_json()
    b = get(app.test_client(), f'/api/state/history/sessions/{SID}', OTHER_TOKEN).get_json()
    assert a['stream']['user_id'] == str(USER_ID)
    assert b['stream']['user_id'] == str(OTHER_USER_ID)
    assert len(a['states']) == 2 and len(b['states']) == 1
    assert a['period']['end'] != b['period']['end']


def test_session_list_rejects_client_user_identity():
    store, _, app, _ = setup_reader()
    assert get(app.test_client(), '/api/state/history/sessions',
               query_string={'user_id': str(OTHER_USER_ID)}).status_code == 400
    assert not store.executions


@pytest.mark.parametrize('sid', ['invalid', 'a' * 32, '00000000-0000-0000-0000-invalidvalue'])
def test_uuid_validation_before_db(sid):
    store, _, app, _ = setup_reader()
    assert get(app.test_client(), f'/api/state/history/sessions/{sid}').status_code == 400
    assert not store.executions


def test_archive_defaults_to_last_snapshot_not_wall_clock_and_scopes_baseline():
    store, _, app, _ = setup_reader()
    store.rows.append(stored_row(time=NOW - timedelta(minutes=6), score=83))
    response = get(app.test_client(), f'/api/state/history/sessions/{SID}')
    assert response.status_code == 200
    payload = response.get_json()
    STATE_HISTORY_VALIDATOR.validate(payload)
    assert payload['period'] == {'start': '2026-10-14T02:55:00Z', 'end': '2026-10-14T03:00:00Z'}
    assert payload['baseline']['score'] == 83
    assert len(payload['states']) == 2
    assert 'metrics' not in payload['states'][0]
    assert payload['stream'] == {'user_id': str(USER_ID), 'session_id': str(SID)}


@pytest.mark.parametrize('minutes, status', [(30, 200), (60, 200), (61, 400)])
def test_recorded_explicit_window_limit(minutes, status):
    _, _, app, _ = setup_reader()
    response = get(app.test_client(), f'/api/state/history/sessions/{SID}', query_string={
        'start': (NOW - timedelta(minutes=minutes)).isoformat(), 'end': NOW.isoformat(),
    })
    assert response.status_code == status


@pytest.mark.parametrize('query', [{'user_id': str(OTHER_USER_ID)}, {'session_id': str(SID)},
                                  {'device_id': 'other'}, {'start': NOW.isoformat()}])
def test_recorded_rejects_identity_queries_and_incomplete_time(query):
    _, _, app, _ = setup_reader()
    assert get(app.test_client(), f'/api/state/history/sessions/{SID}', query_string=query).status_code == 400


def test_empty_list_and_empty_window_are_success_not_missing():
    store, _, app, _ = setup_reader()
    store.rows = []
    assert get(app.test_client(), '/api/state/history/sessions').get_json()['sessions'] == []
    store.rows.append(stored_row())
    response = get(app.test_client(), f'/api/state/history/sessions/{SID}', query_string={
        'start': (NOW + timedelta(minutes=1)).isoformat(), 'end': (NOW + timedelta(minutes=2)).isoformat(),
    })
    assert response.status_code == 200
    assert response.get_json()['states'] == []


@pytest.mark.parametrize('path', ['/api/state/history/sessions', f'/api/state/history/sessions/{SID}'])
def test_db_failure_is_safe_503_and_realtime_still_emits(path):
    store, _, app, socket = setup_reader()
    store.error = RuntimeError('secret driver credentials')
    response = get(app.test_client(), path)
    assert response.status_code == 503
    assert response.get_json()['error']['code'] == 'history_unavailable'
    assert 'secret' not in response.get_data(as_text=True)
    owner = socket.test_client(app, auth={'token': USER_TOKEN})
    producer = socket.test_client(app, auth={'token': SENSOR_TOKEN})
    app.test_client().post('/api/measurement/start', headers={'Authorization': 'Bearer ' + USER_TOKEN})
    owner.get_received()
    producer.emit('sensor_data', chair_payload())
    assert any(event['name'] == 'state' for event in owner.get_received())
    app.test_client().post('/api/measurement/stop', headers={'Authorization': 'Bearer ' + USER_TOKEN})
    owner.disconnect()
    producer.disconnect()


def stopped_session_fixture():
    """Real Fusion/persistence mapping into a durable DB double, then a new app."""
    store = SnapshotStore()
    reader = StateHistoryReader(connection_factory=store.connect)
    persistence = StatePersistence(store, 5)
    app, socket = make_app(history_reader=reader, state_persistence=persistence)
    owner = socket.test_client(app, auth={'token': USER_TOKEN})
    producer = socket.test_client(app, auth={'token': SENSOR_TOKEN})
    client = app.test_client()
    started = client.post('/api/measurement/start', headers={'Authorization': 'Bearer ' + USER_TOKEN})
    assert started.status_code == 201
    sid = started.get_json()['measurement']['session_id']
    for second in range(26):
        producer.emit('sensor_data', chair_payload(t=NOW.timestamp() + second))
    assert store.rows and any(event['name'] == 'state' for event in owner.get_received())
    assert client.post('/api/measurement/stop', headers={'Authorization': 'Bearer ' + USER_TOKEN}).status_code == 200
    saved = len(store.rows)
    producer.emit('sensor_data', chair_payload(t=NOW.timestamp() + 27))
    assert len(store.rows) == saved
    assert get(client, '/api/state/history').status_code == 409
    before = get(client, f'/api/state/history/sessions/{sid}').get_json()
    owner.disconnect()
    producer.disconnect()
    restarted, _ = make_app(history_reader=reader)
    assert restarted.extensions['measurement_sessions'].active() is None
    assert get(restarted.test_client(), '/api/state/history').status_code == 409
    after = get(restarted.test_client(), f'/api/state/history/sessions/{sid}').get_json()
    assert before == after
    return get(restarted.test_client(), '/api/state/history/sessions').get_json(), after


def test_stop_then_restart_preserves_history_without_runtime_registry():
    sessions, snapshots = stopped_session_fixture()
    HISTORY_SESSIONS_VALIDATOR.validate(sessions)
    STATE_HISTORY_VALIDATOR.validate(snapshots)
    assert sessions['sessions'][0]['session_id'] == snapshots['stream']['session_id']
    assert snapshots['states']


def test_session_reader_resource_cleanup_and_not_found():
    cursor = FakeCursor(error=RuntimeError('secret'))
    connection = FakeConnection(cursor)
    reader = StateHistoryReader(connection_factory=lambda: connection)
    with pytest.raises(HistoryUnavailable, match='recorded session query failed'):
        reader.list_sessions(USER_ID)
    assert cursor.closed and connection.closed
    empty = StateHistoryReader(connection_factory=lambda: FakeConnection(FakeCursor()))
    with pytest.raises(HistorySessionNotFound):
        empty.get_session(USER_ID, SID)
    assert parse_session_id(str(SID).upper()) == SID
