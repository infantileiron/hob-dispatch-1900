"""End-to-end checks for batch import against a real PostgreSQL.

Run with DB_* pointing at a disposable database that already has migrate + seed_demo applied:

    DB_HOST=... python -m pytest tests/test_import_api.py -q
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import psycopg

if not os.getenv('DB_HOST'):
    pytest.skip('DB_HOST not set; integration tests need PostgreSQL', allow_module_level=True)

from fastapi.testclient import TestClient

from src.main import app
from src.models.db import connect
from src.schemas.inputs import ImportInput

TZ = timezone(timedelta(hours=8))


def iso(moment):
    return moment.isoformat()


@pytest.fixture(scope='module')
def client():
    with TestClient(app) as test_client:
        login = test_client.post('/session', json={'email': 'recorder@demo.local', 'password': 'HobShift#21'})
        assert login.status_code == 200, login.text
        test_client.headers['Authorization'] = f"Bearer {login.json()['token']}"
        yield test_client


def open_shift(client, *, minutes=60):
    """Create a disposable open shift with a far-future due time and its own window."""
    due = datetime.now(timezone.utc) + timedelta(days=1)
    with connect() as db:
        station = db.execute("SELECT id FROM stations WHERE code='H03'").fetchone()
        rule = db.execute("SELECT id FROM rules WHERE station_id=%s AND shift_kind='morning' "
                          'ORDER BY version DESC LIMIT 1', (station['id'],)).fetchone()
        actor = db.execute("SELECT id FROM accounts WHERE email='recorder@demo.local'").fetchone()
        while True:
            nonce = int(uuid4().hex[:10], 16)
            start = datetime(2025, 1, 1, tzinfo=TZ) + timedelta(minutes=nonce % 500_000)
            code = f'HL-TEST-{uuid4().hex[:8].upper()}'
            try:
                with db.transaction():
                    shift = db.execute(
                        '''INSERT INTO shifts (code,station_id,shift_kind,starts_at,ends_at,due_at,rule_id,created_by)
                           VALUES (%s,%s,'morning',%s,%s,%s,%s,%s) RETURNING *''',
                        (code, station['id'], start, start + timedelta(minutes=minutes), due,
                         rule['id'], actor['id'])).fetchone()
                return code, start, shift
            except psycopg.errors.UniqueViolation:
                continue


def readings(code, start, meters, logs):
    return {
        'code': code,
        'meter_readings': [{'sampled_at': iso(start + timedelta(minutes=m)), 'value_m3': v, 'meter_no': 'M1'}
                           for m, v in meters],
        'ignition_logs': [{'sampled_at': iso(start + timedelta(minutes=m)), 'burner_minutes': b}
                          for m, b in logs],
    }


def body(entries, key=None):
    return {'idempotency_key': key or f'test-{uuid4().hex}', 'shifts': entries}


def test_preview_then_submit_is_idempotent(client):
    code, start, shift = open_shift(client)
    entry = readings(code, start, [(0, '10.000'), (15, '10.500'), (30, '11.000')], [(0, 20), (15, 20)])
    request = body([entry])

    preview = client.post('/imports/preview', json=request)
    assert preview.status_code == 200, preview.text
    item = preview.json()['shifts'][0]
    assert item['state'] == 'ready'
    assert [slot['status'] for slot in item['slots']] == ['import', 'import', 'blank', 'blank']
    assert item['slots'][2]['reason'] == 'missing_end'
    assert item['slots'][3]['reason'] == 'missing_start'

    first = client.post('/imports', json=request)
    assert first.status_code == 201, first.text
    batch = first.json()['batch']
    assert batch['status'] == 'completed'
    assert first.json()['shifts'][0]['status'] == 'imported'
    assert first.json()['shifts'][0]['imported_slots'] == 2

    # Replaying the same key must not add readings or process anything again.
    second = client.post('/imports', json=request)
    assert second.status_code == 201
    assert second.json()['batch']['id'] == batch['id']

    detail = client.get(f"/shifts/{shift['id']}").json()
    assert len(detail['readings']) == 2
    assert {Decimal(str(row['gas_m3'])) for row in detail['readings']} == {Decimal('0.500')}

    history = client.get(f"/shifts/{shift['id']}/imports").json()
    assert len(history) == 1
    assert history[0]['status'] == 'imported'

    # A different key over already-filled slots is a manual conflict, whole shift returned.
    again = client.post('/imports/preview', json=body([entry]))
    assert again.json()['shifts'][0]['state'] == 'rejected'
    assert again.json()['shifts'][0]['reasons'][0]['code'] == 'manual_conflict'
    # The preview must not pretend any slot would be imported.
    assert {slot['status'] for slot in again.json()['shifts'][0]['slots']} == {'blank'}
    assert again.json()['shifts'][0]['totals']['import_slots'] == 0


def test_same_key_different_payload_conflicts(client):
    code, start, _ = open_shift(client)
    key = f'test-{uuid4().hex}'
    a = readings(code, start, [(0, '5.000'), (15, '5.500')], [(0, 20)])
    b = readings(code, start, [(0, '5.000'), (15, '5.900')], [(0, 20)])
    assert client.post('/imports', json=body([a], key)).status_code == 201
    conflict = client.post('/imports', json=body([b], key))
    assert conflict.status_code == 409


def make_frozen_shift():
    """A shift whose scheduled delivery has already passed, so its ledger is closed."""
    nonce = int(uuid4().hex[:10], 16)
    start = datetime(2025, 2, 1, tzinfo=TZ) + timedelta(minutes=nonce % 500_000)
    code = f'HL-FROZEN-{uuid4().hex[:8].upper()}'
    with connect() as db:
        station = db.execute("SELECT id FROM stations WHERE code='H03'").fetchone()
        rule = db.execute("SELECT id FROM rules WHERE station_id=%s AND shift_kind='morning' "
                          'ORDER BY version DESC LIMIT 1', (station['id'],)).fetchone()
        actor = db.execute("SELECT id FROM accounts WHERE email='recorder@demo.local'").fetchone()
        db.execute('''INSERT INTO shifts (code,station_id,shift_kind,starts_at,ends_at,due_at,rule_id,created_by)
                      VALUES (%s,%s,'morning',%s,%s,clock_timestamp() - interval '1 minute',%s,%s)''',
                   (code, station['id'], start, start + timedelta(minutes=60), rule['id'], actor['id']))
    return code, start


def test_frozen_shift_rejects_whole_batch_but_others_import(client):
    frozen_code, frozen_start = make_frozen_shift()
    fresh_code, start, _ = open_shift(client)

    frozen_entry = {'code': frozen_code,
                    'meter_readings': [{'sampled_at': iso(frozen_start - timedelta(minutes=15)),
                                        'value_m3': '0.100'}],
                    'ignition_logs': [{'sampled_at': iso(frozen_start), 'burner_minutes': 10}]}
    fresh_entry = readings(fresh_code, start, [(0, '3.000'), (15, '3.250')], [(0, 20)])
    result = client.post('/imports', json=body([frozen_entry, fresh_entry]))
    assert result.status_code == 201, result.text
    view = result.json()
    assert view['batch']['status'] == 'partial'
    by_code = {row['code']: row for row in view['shifts']}
    assert by_code[frozen_code]['status'] == 'rejected'
    assert by_code[frozen_code]['reason'] == 'shift_frozen'
    assert by_code[fresh_code]['status'] == 'imported'


def test_unknown_shift_is_unmatched(client):
    fresh_code, start, _ = open_shift(client)
    entry = readings(fresh_code, start, [(0, '1.000'), (15, '1.200')], [(0, 20)])
    result = client.post('/imports/preview', json=body([entry, {'code': 'HL-NOPE-999', 'meter_readings': [], 'ignition_logs': []}]))
    assert result.status_code == 200
    assert result.json()['unmatched'] == ['HL-NOPE-999']


def test_manual_entry_after_submit_sends_shift_back(client):
    code, start, shift = open_shift(client)
    entry = readings(code, start, [(0, '7.000'), (15, '7.500'), (30, '8.000')], [(0, 20), (15, 20)])
    from src.services import imports

    with connect() as db:
        actor = db.execute("SELECT * FROM accounts WHERE email='recorder@demo.local'").fetchone()
        request = ImportInput(**body([entry]))
        batch_id = imports.submit(db, actor, request)
        target = db.execute('SELECT shift_id FROM import_batch_shifts WHERE batch_id=%s',
                            (batch_id,)).fetchone()
        # A concurrent manual entry lands after submit but before the batch runs.
        db.execute('''INSERT INTO readings (shift_id,sampled_at,gas_m3,burner_minutes,recorded_by)
                      VALUES (%s,%s,0.400,20,%s)''', (target['shift_id'], start, actor['id']))

    imports.run_batch(batch_id, actor['email'])
    with connect() as db:
        view = imports.batch_view(db, batch_id)
    row = view['shifts'][0]
    assert row['status'] == 'rejected'
    assert row['reason'] == 'manual_conflict'
    detail = client.get(f"/shifts/{shift['id']}").json()
    # Only the manual reading remains: the batch wrote nothing for that shift.
    assert len(detail['readings']) == 1
    assert Decimal(str(detail['readings'][0]['gas_m3'])) == Decimal('0.400')


def test_interrupted_batch_resumes_without_repeating_imports(client):
    code, start, _ = open_shift(client)
    from src.services import imports

    entry = readings(code, start, [(0, '4.000'), (15, '4.500'), (30, '5.000')], [(0, 20), (15, 20)])
    with connect() as db:
        actor = db.execute("SELECT * FROM accounts WHERE email='recorder@demo.local'").fetchone()
        batch_id = imports.submit(db, actor, ImportInput(**body([entry])))
    # Simulate a process death before any work: the row is still pending.
    imports.run_batch(batch_id, actor['email'])
    with connect() as db:
        imported_at = db.execute('SELECT finished_at FROM import_batch_shifts WHERE batch_id=%s',
                                 (batch_id,)).fetchone()['finished_at']
        readings_before = db.execute('SELECT count(*) AS n FROM readings r JOIN import_batch_shifts b '
                                     'ON b.shift_id=r.shift_id WHERE b.batch_id=%s', (batch_id,)).fetchone()['n']
    imports.run_batch(batch_id, actor['email'])
    with connect() as db:
        again = db.execute('SELECT finished_at FROM import_batch_shifts WHERE batch_id=%s',
                           (batch_id,)).fetchone()['finished_at']
        readings_after = db.execute('SELECT count(*) AS n FROM readings r JOIN import_batch_shifts b '
                                    'ON b.shift_id=r.shift_id WHERE b.batch_id=%s', (batch_id,)).fetchone()['n']
        status = db.execute('SELECT status FROM import_batches WHERE id=%s', (batch_id,)).fetchone()['status']
    assert again == imported_at
    assert readings_after == readings_before == 2
    assert status == 'completed'


def test_resume_leaves_settled_rows_alone(client):
    code, start, _ = open_shift(client)
    entry = readings(code, start, [(0, '2.000'), (15, '2.300')], [(0, 20)])
    created = client.post('/imports', json=body([entry])).json()
    batch_id = created['batch']['id']
    created_at = created['shifts'][0]['finished_at']
    resumed = client.post(f'/imports/{batch_id}/resume')
    assert resumed.status_code == 200
    assert resumed.json()['shifts'][0]['finished_at'] == created_at
    detail = client.get(f"/shifts/{created['shifts'][0]['shift_id']}").json()
    assert len(detail['readings']) == 1
