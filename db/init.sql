-- Schema for edops. Status values and transitions are defined in docs/design.md section 2.

CREATE TABLE IF NOT EXISTS orders (
    id               UUID PRIMARY KEY,
    customer_id      TEXT NOT NULL,
    idempotency_key  TEXT,
    request_hash     TEXT,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    items            JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(items) = 'array'),
    status           TEXT NOT NULL CHECK (status IN ('PENDING', 'CONFIRMED', 'FAILED')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A key without a hash could never detect a different body reusing it.
    CHECK ((idempotency_key IS NULL) = (request_hash IS NULL)),
    UNIQUE (customer_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS outbox (
    id            BIGSERIAL PRIMARY KEY,
    -- Stable event id for consumer dedupe; survives re-publishing by the relay.
    message_id    UUID NOT NULL UNIQUE,
    aggregate_id  UUID NOT NULL,
    event_type    TEXT NOT NULL,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS outbox_unpublished_idx
    ON outbox (id) WHERE published_at IS NULL;

-- DM-1: consumer dedupe. Inserted in the same transaction as the consumer's business write.
CREATE TABLE IF NOT EXISTS processed_messages (
    consumer      TEXT NOT NULL,
    message_id    UUID NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, message_id)
);

-- DM-2: at most one charge and one refund per order, so net charges per order are <= 1
-- even if dedupe above this layer fails (INV-2).
CREATE TABLE IF NOT EXISTS payments (
    id            BIGSERIAL PRIMARY KEY,
    order_id      UUID NOT NULL REFERENCES orders (id),
    kind          TEXT NOT NULL CHECK (kind IN ('charge', 'refund')),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (order_id, kind)
);

-- DM-5
CREATE TABLE IF NOT EXISTS inventory (
    sku    TEXT PRIMARY KEY,
    stock  INTEGER NOT NULL CHECK (stock >= 0)
);

-- DM-3: release is UPDATE ... WHERE status = 'reserved', so a second release is a no-op (INV-3).
CREATE TABLE IF NOT EXISTS reservations (
    order_id     UUID NOT NULL REFERENCES orders (id),
    sku          TEXT NOT NULL REFERENCES inventory (sku),
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    status       TEXT NOT NULL CHECK (status IN ('reserved', 'released')),
    reserved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at  TIMESTAMPTZ,
    CHECK ((status = 'released') = (released_at IS NOT NULL)),
    PRIMARY KEY (order_id, sku)
);

-- DM-4: one saga row per order.
CREATE TABLE IF NOT EXISTS saga_state (
    order_id    UUID PRIMARY KEY REFERENCES orders (id),
    step        TEXT NOT NULL CHECK (step IN (
                    'PAYMENT_REQUESTED', 'STOCK_REQUESTED', 'REFUND_REQUESTED',
                    'CONFIRMED', 'FAILED', 'COMPENSATED')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Keep in sync with INVENTORY_SEED in tests/conftest.py.
INSERT INTO inventory (sku, stock) VALUES
    ('SKU-1', 10),
    ('SKU-2', 5),
    ('SKU-EMPTY', 0)
ON CONFLICT (sku) DO NOTHING;
