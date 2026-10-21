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

-- Lookups by order (the demo dashboard polls these every second) and by message id (the join
-- from outbox to processed_messages, whose primary key leads with consumer, not message_id).
CREATE INDEX IF NOT EXISTS outbox_aggregate_idx ON outbox (aggregate_id);
CREATE INDEX IF NOT EXISTS orders_created_at_idx ON orders (created_at DESC);

-- Wakes the relay as soon as a row commits, so it does not have to poll on a short timer.
-- NOTIFY is delivered at commit, never for a rolled-back insert.
CREATE OR REPLACE FUNCTION notify_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('outbox', '');
    RETURN NULL;
END;
$$;

CREATE OR REPLACE TRIGGER outbox_notify
    AFTER INSERT ON outbox
    FOR EACH STATEMENT EXECUTE FUNCTION notify_outbox();

-- DM-1: consumer dedupe. Inserted in the same transaction as the consumer's business write.
CREATE TABLE IF NOT EXISTS processed_messages (
    consumer      TEXT NOT NULL,
    message_id    UUID NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, message_id)
);

CREATE INDEX IF NOT EXISTS processed_messages_message_idx ON processed_messages (message_id);

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

-- A refund needs a charge for the same order, for at least the refunded amount. A trigger
-- rather than a foreign key: an FK would need a refund_of column on every refund row.
CREATE OR REPLACE FUNCTION check_refund_has_charge() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    charged INTEGER;
BEGIN
    SELECT amount_cents INTO charged FROM payments WHERE order_id = NEW.order_id AND kind = 'charge';
    IF charged IS NULL THEN
        RAISE EXCEPTION 'refund for order % has no charge', NEW.order_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NEW.amount_cents > charged THEN
        RAISE EXCEPTION 'refund % exceeds charge % for order %', NEW.amount_cents, charged, NEW.order_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER payments_refund_has_charge
    BEFORE INSERT ON payments
    FOR EACH ROW WHEN (NEW.kind = 'refund') EXECUTE FUNCTION check_refund_has_charge();

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
                    'PAYMENT_REQUESTED', 'STOCK_REQUESTED', 'RELEASE_REQUESTED',
                    'REFUND_REQUESTED', 'CONFIRMED', 'FAILED', 'COMPENSATED')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Keep in sync with INVENTORY_SEED in tests/conftest.py.
INSERT INTO inventory (sku, stock) VALUES
    ('SKU-1', 10),
    ('SKU-2', 5),
    ('SKU-EMPTY', 0)
ON CONFLICT (sku) DO NOTHING;
