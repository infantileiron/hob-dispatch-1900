CREATE TABLE IF NOT EXISTS import_batches (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    payload_hash text NOT NULL,
    payload jsonb NOT NULL,
    actor_id bigint NOT NULL REFERENCES accounts(id),
    status text NOT NULL CHECK (status IN ('importing', 'completed', 'partial', 'failed', 'empty')),
    unmatched jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE TABLE IF NOT EXISTS import_batch_shifts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id bigint NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    shift_id bigint NOT NULL REFERENCES shifts(id),
    input jsonb NOT NULL,
    slots jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'imported', 'rejected', 'empty')),
    reason text,
    imported_slots integer NOT NULL DEFAULT 0 CHECK (imported_slots >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (batch_id, shift_id)
);
CREATE INDEX IF NOT EXISTS import_batch_shifts_pending
    ON import_batch_shifts (batch_id, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS import_batch_shifts_shift
    ON import_batch_shifts (shift_id, id DESC);
