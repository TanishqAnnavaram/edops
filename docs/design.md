# Design: Event-Driven Order Processing

Write this yourself. Rough is fine. Nothing here ships without your own words.

## 1. Problem and invariants
An order passes through three processes (API, payment, inventory) connected by a broker that
can redeliver, reorder or lose in-flight messages, and any process can crash at any line.
Three things must never happen:

- **INV-1 No lost order.** Once POST /orders returns 201, the order is eventually CONFIRMED or
  explicitly FAILED. It never sits in the database with no event that moves it forward.
- **INV-2 No double charge.** One order produces at most one net charge, however many times a
  request is retried or a message is redelivered.
- **INV-3 No orphaned reservation.** Stock reserved for an order that ends FAILED is released,
  exactly once.

How each is enforced:
- INV-1: the order row, its saga_state row and its OrderCreated outbox row commit in one
  transaction. If the process dies after the first INSERT and before the commit, Postgres rolls
  the whole transaction back: there is no order and no event, and the client got no 201, so it
  retries with the same Idempotency-Key. The relay publishes outbox rows until each is marked.
- INV-2: Idempotency-Key at the API (unique per customer), processed_messages dedupe at each
  consumer, and `UNIQUE (order_id, kind)` on payments, so a second charge is a constraint
  violation even if the code above it is wrong.
- INV-3: reservations carry a status; release is `UPDATE ... WHERE status = 'reserved'`, so a
  second release changes nothing.

## 2. Order state machine
Two levels, owned by the orchestrator:
- **orders.status** (coarse, what a customer sees): `PENDING`, `CONFIRMED`, `FAILED`.
- **saga_state.step** (detail, one row per order, `order_id` is the PK): `PAYMENT_REQUESTED`,
  `STOCK_REQUESTED`, `REFUND_REQUESTED`, `CONFIRMED`, `FAILED`, `COMPENSATED`.

| # | Trigger | step: from -> to | orders.status | Writes Postgres | Publishes (outbox row) |
|---|---------|------------------|---------------|-----------------|------------------------|
| T1 | POST /orders | (none) -> PAYMENT_REQUESTED | PENDING | yes | **yes: OrderCreated** |
| T2 | PaymentSucceeded | PAYMENT_REQUESTED -> STOCK_REQUESTED | PENDING | yes | **yes: ReserveStock** |
| T3 | PaymentFailed | PAYMENT_REQUESTED -> FAILED | FAILED | yes | no |
| T4 | StockReserved | STOCK_REQUESTED -> CONFIRMED | CONFIRMED | yes | no |
| T5 | StockRejected | STOCK_REQUESTED -> REFUND_REQUESTED | PENDING | yes | **yes: RefundPayment** |
| T6 | PaymentRefunded | REFUND_REQUESTED -> COMPENSATED | FAILED | yes | no |

Rules:
- Every transition is one transaction. "Publishes" means it inserts an outbox row in that same
  transaction; nothing talks to the broker directly.
- Terminal transitions (T3, T4, T6) update orders.status and saga_state.step in the same
  transaction, so the two levels can never disagree.
- An event that does not match the current step (duplicate, late, out of order) is a logged
  no-op. That is what makes redelivery to the orchestrator safe.
- INV-3 exposure: payment runs before reservation and a reservation is all-or-nothing, so
  nothing after T4 can fail. A release is only needed when StockReserved arrives for an order
  that is already FAILED (late delivery) or for a future cancel. The orchestrator must answer
  that case with a ReleaseStock command, and the INV-3 test must construct it deliberately.

Implemented so far: T1 only.

## 3. Failure catalog
| # | Failure | What happens | How handled | Verdict |
|---|---------|--------------|-------------|---------|
At least one entry should end with "handled badly".

## 4. Decisions and rejected alternatives
- Broker (RabbitMQ) vs Postgres-as-a-queue: choice, reason, what I rejected.
- Orchestrated vs choreographed saga: choice, reason, what I rejected.
- Delivery semantics (at-least-once) and why:
- **Outbox vs dual write.** Chose the transactional outbox: the order and its event commit in
  one Postgres transaction and a relay publishes afterwards. Rejected dual write (commit, then
  publish): a crash between the two leaves an order with no event (INV-1), and publishing first
  can announce an order that then rolls back. Cost: an extra table, a relay process, and
  duplicate publishes when the relay dies after publishing but before marking the row, which
  consumers must dedupe anyway.
- **items as JSONB vs a join table.** Chose an `items JSONB` column on orders. The inventory
  worker only reads (sku, quantity) pairs from the event, and items are never queried or
  updated on their own. Rejected an order_items table: more schema and joins for no reader that
  needs them. Cost: no foreign key from an item's SKU to inventory, so an unknown SKU is caught
  by the inventory worker (StockRejected), not by the database.
- **Idempotency keys per customer vs global.** Chose `UNIQUE (customer_id, idempotency_key)`.
  With a global key, one customer's key would block another customer's order, or worse, replay
  it back to them. The key is required at the HTTP layer (400 without it), because Postgres
  treats NULLs as distinct and an optional key would silently allow duplicates. The request
  body is hashed (sha256 of canonical JSON) and stored as request_hash: same key and hash
  returns the original 201 rebuilt from the stored row; same key and a different hash returns
  409. Item order is part of the hash, so a retry that lists the same items in a different
  order gets 409 by design: a false conflict is safe, a false replay would not be.

## 5. Known future work
- Retries and DLQ, replay CLI, load test, reconciliation job.

## 6. Parking lot (ideas not in this build)
