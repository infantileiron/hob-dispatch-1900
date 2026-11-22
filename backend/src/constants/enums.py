from enum import StrEnum


class Role(StrEnum):
    MANAGER = 'manager'
    RECORDER = 'recorder'
    OBSERVER = 'observer'


class ShiftKind(StrEnum):
    MORNING = 'morning'
    EVENING = 'evening'


class Mailbox(StrEnum):
    KITCHEN = 'kitchen'
    OPERATIONS = 'operations'


class Outcome(StrEnum):
    ALERTED = 'alerted'
    COOLDOWN = 'cooldown'
    WITHIN_RULE = 'within_rule'
    UNMEASURABLE = 'unmeasurable'


class ImportStatus(StrEnum):
    IMPORTING = 'importing'
    COMPLETED = 'completed'
    PARTIAL = 'partial'
    FAILED = 'failed'
    EMPTY = 'empty'


class ImportShiftStatus(StrEnum):
    PENDING = 'pending'
    IMPORTED = 'imported'
    REJECTED = 'rejected'
    EMPTY = 'empty'


class SlotStatus(StrEnum):
    IMPORT = 'import'
    BLANK = 'blank'


class PreviewState(StrEnum):
    READY = 'ready'
    REJECTED = 'rejected'


SLOT_MINUTES = 15
