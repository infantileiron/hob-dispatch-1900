import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from src.constants.enums import (SLOT_MINUTES, ImportShiftStatus, ImportStatus, PreviewState,
                                 SlotStatus)
from src.models.db import audit, connect
from src.services.evaluate import normalized
from src.utils.encoding import payload

log = logging.getLogger('imports')

# Shifts handled per call to run_batch. The API call processes a whole submitted batch;
# the worker-free loop can be re-entered with POST /imports/{id}/resume after an
# interruption, and rows already settled are never reprocessed.
BATCH_CHUNK = 200

SHIFT_SQL = '''
SELECT s.*, st.code AS station_code, st.label AS station_label, st.burner_count
FROM shifts s JOIN stations st ON st.id=s.station_id
'''

BLANK_REASONS = {
    'slot_not_ended': '时格尚未结束，暂不可入账',
    'missing_start': '缺端点：时格起点没有抄表记录',
    'missing_end': '缺端点：时格终点没有抄表记录',
    'cross_meter': '跨表：时格两端抄表表号不同',
    'meter_reset': '跨表：时格终点表值小于起点，疑似换表或回零',
    'ambiguous_reading': '同一时刻存在表值或表号不一致的抄表记录',
    'misaligned_reading': '时格内有不对齐刻度的抄表记录，无法确认端点',
    'missing_burner_minutes': '缺炉分钟：时格内没有点火日志',
    'zero_burner_with_gas': '时格有表差但合并后的点火炉分钟为零',
    'burner_capacity': '点火炉分钟超过该时格灶眼容量',
}
SHIFT_REASONS = {
    'shift_missing': '班次编号不存在',
    'shift_frozen': '已到计划投递时刻或班报已生成，用气账已封账',
    'manual_conflict': '并发手录或其它入账已占用该班时格，整班退回',
    'empty_plan': '没有可入账的时格',
}
EMPTY_TOTALS = {'gas_m3': None, 'burner_minutes': None, 'intensity': None,
                'import_slots': 0, 'blank_slots': 0}


def _reason(code, detail=''):
    text = BLANK_REASONS.get(code) or SHIFT_REASONS.get(code) or code
    return {'code': code, 'text': text + (f'（{detail}）' if detail else '')}


def _blank(code):
    return {'status': SlotStatus.BLANK, 'reason': code, 'reason_text': BLANK_REASONS[code]}


def _stamp(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _slot_pairs(shift):
    pairs, cursor = [], shift['starts_at']
    while cursor < shift['ends_at']:
        pairs.append((cursor, min(cursor + timedelta(minutes=SLOT_MINUTES), shift['ends_at'])))
        cursor += timedelta(minutes=SLOT_MINUTES)
    return pairs


def _plan(db, shift, data):
    """Derive every slot from the submitted logs without writing anything.

    Meter records are cumulative, so a slot is the difference between the readings at its
    two edges and may arrive in any order. Ignition records inside one slot are merged by
    summing their burner minutes. Problems are reported per slot rather than rejecting a
    whole shift, except for shift-level blocks (freeze, concurrent manual entry).
    """
    now = db.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    pairs = _slot_pairs(shift)
    starts_at, ends_at = shift['starts_at'], shift['ends_at']

    edges, misaligned = {}, set()
    for record in data.get('meter_readings', []):
        stamp = _stamp(record['sampled_at'])
        on_edge = stamp == ends_at or (
            starts_at <= stamp < ends_at and (stamp - starts_at) % timedelta(minutes=SLOT_MINUTES) == timedelta(0))
        if stamp < starts_at or stamp > ends_at:
            continue  # Another shift's reading; it cannot be an edge here.
        if not on_edge:
            misaligned.add(stamp)
            continue
        edge = edges.setdefault(stamp, {'values': set(), 'meters': set()})
        edge['values'].add(Decimal(str(record['value_m3'])))
        if record.get('meter_no'):
            edge['meters'].add(record['meter_no'])

    buckets = [[] for _ in pairs]
    for record in data.get('ignition_logs', []):
        stamp = _stamp(record['sampled_at'])
        if not starts_at <= stamp < ends_at:
            continue  # A log outside the half-open window belongs to another shift.
        index = int((stamp - starts_at).total_seconds() // (SLOT_MINUTES * 60))
        if index < len(buckets):
            buckets[index].append(int(record['burner_minutes']))

    slots, imports = [], []
    gas_total = Decimal(0)
    minute_total = 0
    for index, (slot_start, slot_end) in enumerate(pairs):
        slot = {'slot_index': index, 'sampled_at': slot_start, 'slot_end': slot_end,
                'status': SlotStatus.BLANK,
                'reason': None, 'reason_text': None, 'gas_m3': None, 'burner_minutes': None,
                'intensity': None, 'start_value': None, 'end_value': None, 'meter_no': None}
        first, last = edges.get(slot_start), edges.get(slot_end)
        if slot_end > now:
            # Mirror the manual-entry rule: an unfinished slot has no settled reading.
            slot.update(_blank('slot_not_ended'))
            slots.append(slot)
            continue
        if any(edge and (len(edge['values']) > 1 or len(edge['meters']) > 1) for edge in (first, last)):
            slot.update(_blank('ambiguous_reading'))
            slots.append(slot)
            continue
        if any(slot_start <= stamp < slot_end for stamp in misaligned):
            slot.update(_blank('misaligned_reading'))
            slots.append(slot)
            continue
        if first is None:
            slot.update(_blank('missing_start'))
            slots.append(slot)
            continue
        if last is None:
            slot.update(_blank('missing_end'))
            slots.append(slot)
            continue
        start_value = next(iter(first['values']))
        end_value = next(iter(last['values']))
        start_meter = next(iter(first['meters']), None)
        end_meter = next(iter(last['meters']), None)
        slot.update(start_value=start_value, end_value=end_value,
                    meter_no=start_meter if start_meter == end_meter else None)
        minutes = sum(buckets[index]) if buckets[index] else None
        if minutes is not None:
            slot['burner_minutes'] = minutes
        if end_value < start_value:
            slot.update(_blank('meter_reset'))
            slots.append(slot)
            continue
        if start_meter and end_meter and start_meter != end_meter:
            slot.update(_blank('cross_meter'))
            slots.append(slot)
            continue
        gas = end_value - start_value
        slot['gas_m3'] = gas
        capacity = int((slot_end - slot_start).total_seconds() // 60) * shift['burner_count']
        if minutes is None:
            slot.update(_blank('missing_burner_minutes'))
            slots.append(slot)
            continue
        if minutes > capacity:
            slot.update(_blank('burner_capacity'))
            slots.append(slot)
            continue
        if minutes == 0 and gas > 0:
            slot.update(_blank('zero_burner_with_gas'))
            slots.append(slot)
            continue
        slot.update(status=SlotStatus.IMPORT, intensity=normalized(gas, minutes))
        imports.append({'sampled_at': slot_start, 'gas_m3': gas, 'burner_minutes': minutes})
        gas_total += gas
        minute_total += minutes
        slots.append(slot)

    existing = {row['sampled_at'] for row in
                db.execute('SELECT sampled_at FROM readings WHERE shift_id=%s', (shift['id'],))}
    conflicts = [slot['sampled_at'] for slot in slots
                 if slot['status'] == SlotStatus.IMPORT and slot['sampled_at'] in existing]
    reasons = []
    if shift['evaluated_at'] or now >= shift['due_at']:
        reasons.append(_reason('shift_frozen'))
    if conflicts:
        reasons.append(_reason('manual_conflict', '、'.join(stamp.isoformat() for stamp in conflicts[:3])))
    return {
        'slots': slots,
        'imports': imports,
        'reasons': reasons,
        'conflicts': conflicts,
        'totals': {'gas_m3': gas_total, 'burner_minutes': minute_total,
                   'intensity': normalized(gas_total, minute_total),
                   'import_slots': len(imports), 'blank_slots': len(slots) - len(imports)},
    }


def _snapshot(reasons, plan):
    return {'reasons': reasons, 'slots': plan['slots'],
            'conflicts': plan['conflicts'], 'totals': plan['totals']}


def _shift_by_code(db, code, lock=False):
    return db.execute(SHIFT_SQL + ' WHERE s.code=%s' + (' FOR UPDATE OF s' if lock else ''),
                      (code,)).fetchone()


def _shift_by_id(db, shift_id, lock=False):
    return db.execute(SHIFT_SQL + ' WHERE s.id=%s' + (' FOR UPDATE OF s' if lock else ''),
                      (shift_id,)).fetchone()


def _entry_data(entry):
    data = entry.model_dump() if hasattr(entry, 'model_dump') else entry
    return {'meter_readings': data.get('meter_readings', []),
            'ignition_logs': data.get('ignition_logs', [])}


def _preview_entry(db, shift, entry):
    plan = _plan(db, shift, _entry_data(entry))
    reasons = list(plan['reasons'])
    if not plan['imports'] and not reasons:
        reasons.append(_reason('empty_plan'))
    if reasons:
        # A blocked shift would write nothing, so the preview must not mark slots as
        # importable either.
        _blank_out(plan, plan['reasons'][0]['code'] if plan['reasons'] else 'empty_plan')
    return {
        'code': shift['code'], 'shift_id': shift['id'], 'station_code': shift['station_code'],
        'station_label': shift['station_label'], 'shift_kind': shift['shift_kind'],
        'starts_at': shift['starts_at'], 'ends_at': shift['ends_at'], 'due_at': shift['due_at'],
        'evaluated_at': shift['evaluated_at'],
        'state': PreviewState.READY if plan['imports'] and not reasons else PreviewState.REJECTED,
        'reasons': reasons, 'slots': plan['slots'], 'conflicts': plan['conflicts'],
        'totals': plan['totals'],
    }


def _validate_unique(body):
    seen = set()
    for entry in body.shifts:
        if entry.code in seen:
            raise HTTPException(422, f'同一批次中班次编号重复：{entry.code}')
        seen.add(entry.code)


def preview(db, body):
    _validate_unique(body)
    shifts, unmatched = [], []
    for entry in body.shifts:
        shift = _shift_by_code(db, entry.code)
        if shift is None:
            unmatched.append(entry.code)
            continue
        shifts.append(_preview_entry(db, shift, entry))
    return payload({'shifts': shifts, 'unmatched': unmatched,
                    'importable': sum(1 for shift in shifts if shift['state'] == 'ready')})


def _canonical(body):
    """Order- and format-insensitive view of a request, used only for the idempotency check."""
    def stamp(value):
        moment = _stamp(value)
        if moment.tzinfo is None:
            raise HTTPException(422, '时间须带时区偏移')
        return moment.astimezone(timezone.utc).isoformat()

    def money(value):
        return format(Decimal(str(value)).quantize(Decimal('0.001')), 'f')

    shifts = []
    for entry in body.shifts:
        shifts.append({
            'code': entry.code,
            'meter_readings': sorted(
                ({'sampled_at': stamp(row.sampled_at), 'value_m3': money(row.value_m3),
                  'meter_no': (row.meter_no or '').strip() or None} for row in entry.meter_readings),
                key=lambda row: (row['sampled_at'], row['meter_no'] or '', row['value_m3'])),
            'ignition_logs': sorted(
                ({'sampled_at': stamp(row.sampled_at), 'burner_minutes': row.burner_minutes}
                 for row in entry.ignition_logs),
                key=lambda row: (row['sampled_at'], row['burner_minutes'])),
        })
    return {'shifts': sorted(shifts, key=lambda entry: entry['code'])}


def _digest(body):
    canonical = json.dumps(_canonical(body), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def submit(db, actor, body):
    _validate_unique(body)
    if not body.idempotency_key:
        raise HTTPException(422, '提交入账须提供幂等键')
    digest = _digest(body)
    created = db.execute('''INSERT INTO import_batches (idempotency_key,payload_hash,payload,actor_id,status)
                            VALUES (%s,%s,%s,%s,'importing')
                            ON CONFLICT (idempotency_key) DO NOTHING RETURNING id''',
                         (body.idempotency_key, digest, Jsonb(payload(body.model_dump())), actor['id'])).fetchone()
    batch = db.execute('SELECT * FROM import_batches WHERE idempotency_key=%s',
                       (body.idempotency_key,)).fetchone()
    if batch['payload_hash'] != digest:
        raise HTTPException(409, '同一幂等键对应不同的导入内容')
    if created:
        unmatched = []
        for entry in body.shifts:
            shift = _shift_by_code(db, entry.code)
            if shift is None:
                unmatched.append(entry.code)
                continue
            db.execute('''INSERT INTO import_batch_shifts (batch_id,shift_id,input)
                          VALUES (%s,%s,%s)''',
                       (batch['id'], shift['id'], Jsonb(payload(_entry_data(entry)))))
        db.execute('UPDATE import_batches SET unmatched=%s WHERE id=%s',
                   (Jsonb(unmatched), batch['id']))
        audit(db, actor['email'], 'import.batch', batch['id'])
    return batch['id']


def _blank_out(plan, code):
    """A shift that is not imported wrote nothing, so no slot may claim to be imported."""
    for slot in plan['slots']:
        if slot['status'] == SlotStatus.IMPORT:
            slot.update(status=SlotStatus.BLANK, reason=code,
                        reason_text=BLANK_REASONS.get(code) or SHIFT_REASONS.get(code, code))
    plan['imports'] = []
    plan['totals']['import_slots'] = 0
    plan['totals']['blank_slots'] = len(plan['slots'])
    return plan


def _settle(db, row, status, reason, plan):
    if status != ImportShiftStatus.IMPORTED:
        _blank_out(plan, reason)
    db.execute('''UPDATE import_batch_shifts SET status=%s, reason=%s, slots=%s,
                  imported_slots=%s, finished_at=clock_timestamp() WHERE id=%s''',
               (status, reason, Jsonb(payload(_snapshot(plan['reasons'], plan))),
                plan['totals']['import_slots'], row['id']))


class ManualConflict(Exception):
    """A manual reading landed on a planned slot after the plan was computed."""

    def __init__(self, plan, stamps):
        super().__init__('manual conflict')
        self.plan = plan
        self.stamps = stamps


def _apply(db, row, shift, plan):
    """Write one plan. Raises ManualConflict so the caller rolls the whole shift back."""
    if not plan['imports']:
        plan['reasons'] = [_reason('empty_plan')]
        _settle(db, row, ImportShiftStatus.EMPTY, 'empty_plan', plan)
        return
    # Re-check the freeze under the shift row lock just before writing, so a shift that
    # reached its scheduled delivery while the batch was being planned is still returned
    # whole instead of landing a late reading.
    if db.execute('SELECT clock_timestamp() >= %s AS frozen', (shift['due_at'],)).fetchone()['frozen']:
        plan['reasons'] = [_reason('shift_frozen')]
        _settle(db, row, ImportShiftStatus.REJECTED, 'shift_frozen', plan)
        return
    stamps = [item['sampled_at'] for item in plan['imports']]
    for item in plan['imports']:
        # ON CONFLICT leaves an existing manual reading untouched and reports no row.
        # Any such hit aborts the shift: a partial import would leave the ledger with
        # neither the batch's nor a coherent manual reading for the shift.
        inserted = db.execute('''INSERT INTO readings (shift_id,sampled_at,gas_m3,burner_minutes,recorded_by)
                                 VALUES (%s,%s,%s,%s,%s) ON CONFLICT (shift_id,sampled_at) DO NOTHING
                                 RETURNING id''',
                              (shift['id'], item['sampled_at'], item['gas_m3'], item['burner_minutes'],
                               row['actor_id'])).fetchone()
        if inserted is None:
            raise ManualConflict(plan, stamps)
    _settle(db, row, ImportShiftStatus.IMPORTED, None, plan)
    audit(db, row['actor_email'], 'import.shift', row['id'])


def _process_row(db, row, shift):
    """Settle one row inside its own transaction; callers hold the shift row lock."""
    if shift is None:
        plan = {'slots': [], 'conflicts': [], 'totals': dict(EMPTY_TOTALS),
                'reasons': [_reason('shift_missing')]}
        _settle(db, row, ImportShiftStatus.REJECTED, 'shift_missing', plan)
        return
    plan = _plan(db, shift, row['input'])
    if plan['reasons']:
        # Rows that a previous attempt already settled never come back here, and a
        # rolled-back attempt leaves no readings, so a conflict here is genuinely manual.
        _settle(db, row, ImportShiftStatus.REJECTED, plan['reasons'][0]['code'], plan)
        return
    _apply(db, row, shift, plan)


def _reject_conflict(row_id, conflict):
    plan = _blank_out(conflict.plan, 'manual_conflict')
    plan['reasons'] = [_reason('manual_conflict',
                               '、'.join(stamp.isoformat() for stamp in conflict.stamps[:3]))]
    plan['conflicts'] = conflict.stamps
    with connect() as db:
        updated = db.execute('''UPDATE import_batch_shifts SET status=%s, reason='manual_conflict',
                      slots=%s, imported_slots=0, finished_at=clock_timestamp()
                      WHERE id=%s AND status=%s RETURNING id''',
                   (ImportShiftStatus.REJECTED, Jsonb(payload(_snapshot(plan['reasons'], plan))),
                    row_id, ImportShiftStatus.PENDING)).fetchone()
        if updated:
            audit(db, 'import', 'import.conflict', row_id)


def run_batch(batch_id, actor_email='import', limit=BATCH_CHUNK):
    attempted, processed = set(), 0
    while processed < limit:
        conflict = None
        with connect() as db:
            row = db.execute('''SELECT r.*, b.actor_id FROM import_batch_shifts r
                                JOIN import_batches b ON b.id=r.batch_id
                                WHERE r.batch_id=%s AND r.status=%s AND r.id <> ALL(%s::bigint[])
                                ORDER BY r.id LIMIT 1 FOR UPDATE SKIP LOCKED''',
                             (batch_id, ImportShiftStatus.PENDING, list(attempted))).fetchone()
            if row is None:
                break
            processed += 1
            attempted.add(row['id'])
            row['actor_email'] = actor_email
            try:
                # The shift row stays locked until this shift's readings and its snapshot
                # commit together. The worker locks the same row, so a shift report sees
                # either none or all of this batch's readings, never a half-imported shift.
                shift = _shift_by_id(db, row['shift_id'], lock=True)
                _process_row(db, row, shift)
            except ManualConflict as error:
                # Must roll back outside the shift lock, then record the rejection with
                # the plan snapshot taken before the conflicting manual entry appeared.
                db.rollback()
                conflict = (row['id'], error)
            except Exception:
                # Roll the whole shift back and leave the row pending, so resuming can
                # retry it. Other shifts in the batch keep their settled results.
                log.exception('Batch %s shift row %s failed; left pending', batch_id, row['id'])
                db.rollback()
        if conflict:
            _reject_conflict(*conflict)
    with connect() as db:
        _finalize(db, batch_id)


def _finalize(db, batch_id):
    rows = db.execute('''SELECT status, count(*) AS count FROM import_batch_shifts
                         WHERE batch_id=%s GROUP BY status''', (batch_id,)).fetchall()
    counts = {row['status']: row['count'] for row in rows}
    if counts.get(ImportShiftStatus.PENDING):
        return
    imported = counts.get(ImportShiftStatus.IMPORTED, 0)
    rejected = counts.get(ImportShiftStatus.REJECTED, 0)
    empty = counts.get(ImportShiftStatus.EMPTY, 0)
    if not rows:
        status = ImportStatus.EMPTY
    elif imported and not rejected and not empty:
        status = ImportStatus.COMPLETED
    elif imported:
        status = ImportStatus.PARTIAL
    elif rejected:
        status = ImportStatus.FAILED
    else:
        status = ImportStatus.EMPTY
    db.execute('''UPDATE import_batches SET status=%s,
                  finished_at=coalesce(finished_at, clock_timestamp()) WHERE id=%s''', (status, batch_id))


def batch_view(db, batch_id):
    batch = db.execute('''SELECT b.*, a.email AS actor_email, a.display_name AS actor_name
                          FROM import_batches b JOIN accounts a ON a.id=b.actor_id
                          WHERE b.id=%s''', (batch_id,)).fetchone()
    if batch is None:
        raise HTTPException(404, '导入批次不存在')
    batch.pop('payload', None)
    rows = db.execute('''SELECT r.id, r.batch_id, r.shift_id, r.status, r.reason, r.imported_slots,
                                r.slots, r.created_at, r.finished_at,
                                r.input,
                                s.code, s.shift_kind, s.starts_at, s.ends_at, s.due_at, s.evaluated_at,
                                st.code AS station_code, st.label AS station_label
                         FROM import_batch_shifts r JOIN shifts s ON s.id=r.shift_id
                         JOIN stations st ON st.id=s.station_id
                         WHERE r.batch_id=%s ORDER BY r.id''', (batch_id,)).fetchall()
    shifts = []
    for row in rows:
        snapshot = row.pop('slots') or {}
        inputs = row.pop('input') or {}
        shifts.append({**row, 'meter_readings': inputs.get('meter_readings', []),
                       'ignition_logs': inputs.get('ignition_logs', []), **snapshot})
    return payload({'batch': batch, 'shifts': shifts})


def recent(db, limit=5):
    rows = db.execute('''SELECT b.id, b.status, b.unmatched, b.created_at, b.finished_at,
                                a.display_name AS actor_name,
                                count(r.id) AS total_shifts,
                                count(r.id) FILTER (WHERE r.status=%s) AS pending_shifts
                         FROM import_batches b JOIN accounts a ON a.id=b.actor_id
                         LEFT JOIN import_batch_shifts r ON r.batch_id=b.id
                         GROUP BY b.id, a.display_name ORDER BY b.id DESC LIMIT %s''',
                      (ImportShiftStatus.PENDING, limit)).fetchall()
    return payload(rows)


def history(db, shift_id, limit=6):
    rows = db.execute('''SELECT r.id, r.batch_id, r.status, r.reason, r.imported_slots, r.slots,
                                r.created_at, r.finished_at, b.status AS batch_status
                         FROM import_batch_shifts r JOIN import_batches b ON b.id=r.batch_id
                         WHERE r.shift_id=%s ORDER BY r.id DESC LIMIT %s''',
                      (shift_id, limit)).fetchall()
    view = []
    for row in rows:
        snapshot = row.pop('slots') or {}
        blanks = {}
        for slot in snapshot.get('slots') or []:
            if slot.get('reason'):
                blanks[slot['reason']] = blanks.get(slot['reason'], 0) + 1
        view.append({**row, 'reasons': snapshot.get('reasons', []),
                     'totals': snapshot.get('totals') or {},
                     'blank_reasons': [{'code': code, 'count': count,
                                        'text': BLANK_REASONS.get(code) or SHIFT_REASONS.get(code, code)}
                                       for code, count in blanks.items()]})
    return payload(view)
