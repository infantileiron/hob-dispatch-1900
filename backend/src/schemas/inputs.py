from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.constants.enums import Mailbox, ShiftKind


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Input):
    email: Annotated[str, Field(min_length=3, max_length=120)]
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RuleInput(Input):
    station_id: Annotated[int, Field(gt=0)]
    shift_kind: ShiftKind
    threshold: Annotated[Decimal, Field(gt=0, max_digits=9, decimal_places=3)]
    consecutive: Annotated[int, Field(ge=2, le=12)]
    cooldown_minutes: Annotated[int, Field(ge=0, le=1440)]
    delay_minutes: Annotated[int, Field(ge=0, le=120)]
    mailbox: Mailbox


class ShiftInput(Input):
    station_id: Annotated[int, Field(gt=0)]
    code: Annotated[str, Field(pattern=r'^[A-Z0-9][A-Z0-9-]{2,39}$')]
    shift_kind: ShiftKind
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class ReadingInput(Input):
    sampled_at: AwareDatetime
    gas_m3: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=3)]
    burner_minutes: Annotated[int, Field(ge=0, le=180)]


class MeterReadingInput(Input):
    sampled_at: AwareDatetime
    value_m3: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=3)]
    meter_no: Annotated[str | None, Field(max_length=40)] = None


class IgnitionLogInput(Input):
    sampled_at: AwareDatetime
    burner_minutes: Annotated[int, Field(ge=0, le=180)]


class ImportShiftInput(Input):
    code: Annotated[str, Field(pattern=r'^[A-Z0-9][A-Z0-9-]{2,39}$')]
    meter_readings: Annotated[list[MeterReadingInput], Field(max_length=500)] = []
    ignition_logs: Annotated[list[IgnitionLogInput], Field(max_length=2000)] = []


class ImportInput(Input):
    # Optional so the same body can be sent to the side-effect-free preview endpoint.
    idempotency_key: Annotated[str, Field(pattern=r'^[A-Za-z0-9._:-]{8,80}$')] | None = None
    shifts: Annotated[list[ImportShiftInput], Field(min_length=1, max_length=200)]
