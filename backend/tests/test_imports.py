from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.services.imports import _plan

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 21, 17, 0, tzinfo=TZ)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDb:
    """Minimal stand-in for the two queries _plan issues."""

    def __init__(self, existing=(), now=None):
        self.existing = list(existing)
        # After every slot has ended but before the shift's due time.
        self.now = now or START + timedelta(minutes=61)
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((' '.join(sql.split()), params))
        if 'clock_timestamp' in sql:
            return Result([{'now': self.now}])
        if 'FROM readings' in sql:
            assert params == (41,)
            return Result([{'sampled_at': stamp} for stamp in self.existing])
        raise AssertionError(sql)


def shift(code='HL-1121-E01', minutes=60, burners=4, due_offset=8):
    end = START + timedelta(minutes=minutes)
    return {'id': 41, 'code': code, 'starts_at': START, 'ends_at': end,
            'due_at': end + timedelta(minutes=due_offset), 'evaluated_at': None,
            'burner_count': burners, 'station_code': 'H01'}


def meter(minutes, value, meter_no='M1'):
    return {'sampled_at': (START + timedelta(minutes=minutes)).isoformat(),
            'value_m3': Decimal(value), 'meter_no': meter_no}


def log(minutes, burner_minutes):
    return {'sampled_at': (START + timedelta(minutes=minutes)).isoformat(),
            'burner_minutes': burner_minutes}


def plan(entries, data, **kwargs):
    db = FakeDb(**kwargs)
    result = _plan(db, shift(**entries), data)
    return result


def test_out_of_order_edges_are_differenced():
    data = {
        'meter_readings': [meter(30, '32.700'), meter(0, '30.600'), meter(60, '33.980'),
                           meter(15, '31.500'), meter(45, '33.340')],
        'ignition_logs': [log(0, 40), log(15, 30), log(30, 40), log(45, 40)],
    }
    result = plan({}, data)
    assert not result['reasons']
    assert [slot['gas_m3'] for slot in result['slots']] == [
        Decimal('0.900'), Decimal('1.200'), Decimal('0.640'), Decimal('0.640')]
    assert result['totals']['gas_m3'] == Decimal('3.380')
    assert result['totals']['burner_minutes'] == 150
    assert result['totals']['import_slots'] == 4


def test_ignition_logs_in_one_slot_are_summed():
    data = {
        'meter_readings': [meter(0, '10.000'), meter(15, '10.500')],
        'ignition_logs': [log(0, 10), log(5, 15), log(14, 5)],
    }
    result = plan({}, data)
    slot = result['slots'][0]
    assert slot['status'] == 'import'
    assert slot['burner_minutes'] == 30
    assert slot['gas_m3'] == Decimal('0.500')
    assert slot['intensity'] == Decimal('1.000')


def test_cross_meter_slot_left_blank_with_reason():
    data = {
        'meter_readings': [meter(0, '10.000', 'M1'), meter(15, '10.500', 'M2'),
                           meter(30, '11.000', 'M2')],
        'ignition_logs': [log(0, 30), log(15, 30)],
    }
    result = plan({}, data)
    assert result['slots'][0]['status'] == 'blank'
    assert result['slots'][0]['reason'] == 'cross_meter'
    assert '跨表' in result['slots'][0]['reason_text']
    assert result['slots'][1]['status'] == 'import'
    assert result['totals']['import_slots'] == 1


def test_missing_endpoint_left_blank_with_reason():
    data = {'meter_readings': [meter(0, '10.000'), meter(30, '10.500')],
            'ignition_logs': [log(0, 30), log(15, 30)]}
    result = plan({}, data)
    assert result['slots'][0]['reason'] == 'missing_end'
    assert result['slots'][1]['reason'] == 'missing_start'
    assert result['slots'][2]['reason'] == 'missing_end'
    assert result['slots'][3]['reason'] == 'missing_start'
    assert '缺端点' in result['slots'][0]['reason_text']


def test_missing_burner_minutes_left_blank_with_reason():
    data = {'meter_readings': [meter(0, '10.000'), meter(15, '10.500')], 'ignition_logs': []}
    result = plan({}, data)
    assert result['slots'][0]['reason'] == 'missing_burner_minutes'
    assert '缺炉分钟' in result['slots'][0]['reason_text']
    assert result['totals']['import_slots'] == 0


def test_meter_reset_is_left_blank():
    data = {'meter_readings': [meter(0, '10.000'), meter(15, '9.500')], 'ignition_logs': [log(0, 30)]}
    result = plan({}, data)
    assert result['slots'][0]['reason'] == 'meter_reset'


def test_concurrent_manual_entry_rejects_whole_shift():
    data = {
        'meter_readings': [meter(0, '10.000'), meter(15, '10.500'), meter(30, '11.000')],
        'ignition_logs': [log(0, 30), log(15, 30)],
    }
    result = plan({}, data, existing={START + timedelta(minutes=15)})
    assert result['totals']['import_slots'] == 2
    assert [reason['code'] for reason in result['reasons']] == ['manual_conflict']
    assert len(result['conflicts']) == 1


def test_frozen_shift_rejects_whole_shift():
    data = {'meter_readings': [meter(0, '10.000'), meter(15, '10.500')], 'ignition_logs': [log(0, 30)]}
    result = plan({}, data, now=START + timedelta(minutes=70))
    assert [reason['code'] for reason in result['reasons']] == ['shift_frozen']


def test_records_outside_window_are_ignored():
    data = {
        'meter_readings': [meter(-15, '9.000'), meter(0, '10.000'), meter(15, '10.500'),
                           meter(30, '11.000'), meter(45, '11.400')],
        'ignition_logs': [log(-15, 60), log(15, 30)],
    }
    result = plan({}, data)
    # The -15 records must not open a preceding slot, and the 17:15 log belongs to
    # slot 1 rather than slot 0.
    assert result['slots'][0]['reason'] == 'missing_burner_minutes'
    assert result['slots'][1]['status'] == 'import'
    assert result['slots'][1]['burner_minutes'] == 30
    assert result['slots'][2]['status'] == 'blank'
    assert result['slots'][2]['reason'] == 'missing_burner_minutes'
    assert result['totals']['import_slots'] == 1


def test_unfinished_slot_is_left_blank():
    data = {'meter_readings': [meter(0, '10.000'), meter(15, '10.500'), meter(30, '11.000')],
            'ignition_logs': [log(0, 30), log(15, 30)]}
    result = plan({}, data, now=START + timedelta(minutes=22))
    assert result['slots'][0]['status'] == 'import'
    assert result['slots'][1]['reason'] == 'slot_not_ended'
    assert '尚未结束' in result['slots'][1]['reason_text']
    assert result['slots'][2]['reason'] == 'slot_not_ended'
    assert result['totals']['import_slots'] == 1


def test_partial_last_slot_uses_short_capacity():
    data = {'meter_readings': [meter(0, '10.000'), meter(10, '10.400')],
            'ignition_logs': [log(0, 25)]}
    result = plan({'minutes': 10, 'burners': 2}, data)
    slot = result['slots'][0]
    assert slot['slot_end'] - slot['sampled_at'] == timedelta(minutes=10)
    assert slot['reason'] == 'burner_capacity'
