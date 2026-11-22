"""The shift report must see either none or all of a batch's readings.

The importer writes every reading of a shift in one transaction while holding that
shift's row lock. The worker takes the same lock before evaluating. This test holds the
lock and writes readings on one connection, then checks a dispatch started meanwhile
stays blocked until commit and only ever evaluates the complete shift.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

if not os.getenv('DB_HOST'):
    pytest.skip('DB_HOST not set; integration tests need PostgreSQL', allow_module_level=True)

from src.models.db import connect
from src.services import scheduler
from src.utils.encoding import payload

TZ = timezone(timedelta(hours=8))


def make_due_shift():
    with connect() as db:
        # Park any seeded but not-yet-evaluated shift so dispatch targets this one only.
        db.execute('UPDATE shifts SET due_at=clock_timestamp() + interval \'1 day\' '
                   "WHERE evaluated_at IS NULL AND due_at <= clock_timestamp()")
        station = db.execute("SELECT id FROM stations WHERE code='H01'").fetchone()
        rule = db.execute("SELECT id FROM rules WHERE station_id=%s AND shift_kind='evening' "
                          'ORDER BY version DESC LIMIT 1', (station['id'],)).fetchone()
        actor = db.execute("SELECT id FROM accounts WHERE email='recorder@demo.local'").fetchone()
        start = datetime(2025, 3, 1, tzinfo=TZ) + timedelta(minutes=uuid4().int % 90_000)
        code = f'HL-RACE-{uuid4().hex[:8].upper()}'
        shift = db.execute('''INSERT INTO shifts (code,station_id,shift_kind,starts_at,ends_at,due_at,rule_id,created_by)
                              VALUES (%s,%s,'evening',%s,%s,clock_timestamp() - interval '1 second',%s,%s)
                              RETURNING id''',
                           (code, station['id'], start, start + timedelta(minutes=60),
                            rule['id'], actor['id'])).fetchone()
    return code, start, shift['id']


def test_dispatch_waits_for_the_whole_import():
    code, start, shift_id = make_due_shift()
    locked, release, dispatched = threading.Event(), threading.Event(), []

    def write_like_import():
        with connect() as db:
            db.execute('SELECT id FROM shifts WHERE id=%s FOR UPDATE', (shift_id,))
            actor = db.execute("SELECT id FROM accounts WHERE email='recorder@demo.local'").fetchone()
            # 0.500 m3 over 20 burner minutes is 1.500 m3/h: exactly at the H01 evening threshold.
            for offset, gas in ((0, '10.000'), (15, '10.500')):
                db.execute('''INSERT INTO readings (shift_id,sampled_at,gas_m3,burner_minutes,recorded_by)
                              VALUES (%s,%s,%s,%s,%s)''',
                           (shift_id, start + timedelta(minutes=offset), gas, 20, actor['id']))
            locked.set()
            release.wait(5)
            # Both readings and the snapshot commit together, like the importer does.

    def run_dispatch():
        locked.wait(5)
        dispatched.append(scheduler.dispatch_one())

    writer = threading.Thread(target=write_like_import)
    worker = threading.Thread(target=run_dispatch)
    writer.start()
    assert locked.wait(5)
    worker.start()
    time.sleep(0.4)
    # The worker cannot have evaluated yet: the importer still holds the shift row lock.
    assert worker.is_alive(), 'dispatch evaluated a half-written shift'
    release.set()
    writer.join(5)
    worker.join(5)
    assert dispatched == [True]

    with connect() as db:
        receipts = db.execute("SELECT evidence FROM deliveries WHERE shift_id=%s AND kind='report'",
                              (shift_id,)).fetchall()
    assert len(receipts) == 1
    evidence = receipts[0]['evidence']
    assert evidence['valid_slots'] == 2
    assert [slot['state'] for slot in evidence['slots'][:2]] == ['over', 'over']
