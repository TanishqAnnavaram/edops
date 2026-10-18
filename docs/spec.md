Spec: Event-Driven Order Processing (edops)

Target: core reliability + saga + live deploy by end of Monday Oct 19, 2026. Stack: Python 3.12, FastAPI, PostgreSQL 16, RabbitMQ, Docker Compose, GitHub Actions. Authorship tags: [Y] you write it, [C] Claude writes it, [B] both.

1. Goal and success test

A small order system whose failure behavior is understood and provable. You can answer without notes: "What happens if the broker redelivers a message after the consumer crashed mid-transaction, and how do you know no card was charged twice?"

2. Invariants (every test traces to one)
INV-1 No lost order: an accepted order is eventually processed or explicitly failed.
INV-2 No double charge: one order produces at most one net charge, however many times messages are redelivered or requests retried.
INV-3 No orphaned reservation: stock reserved for an order that fails or is refunded is released exactly once.
3. Scope

In: outbox, relay, two workers, idempotency (API and consumer), orchestrated saga, compensation, structured logs, CI, live deploy, one chaos test minimum. Deferred (listed as future work): retries with backoff and jitter, DLQ and replay CLI, load test, reconciliation job, two more chaos tests. Cut for good: Redis, OpenTelemetry, Prometheus, Grafana, more than three services.

4. Architecture
Postgres is the source of truth. RabbitMQ carries events only.
Processes: order-api, outbox-relay, payment-worker, inventory-worker, plus the saga orchestrator (decide in design.md whether it lives in its own process or inside the relay or a worker; justify it).
Delivery semantics: at-least-once. Duplicates are expected and handled by idempotent consumers.
5. Data model

Existing (db/init.sql): orders, outbox. To add (you design the columns in design.md first):

DM-1 processed_messages (consumer dedupe): unique per (consumer, message id).
DM-2 payments: one row per order charge or refund, with a uniqueness rule that makes a double charge impossible at the database level.
DM-3 reservations: stock reserved per order, with release state.
DM-4 saga_state (or equivalent on orders): current step, persisted at every transition.
DM-5 inventory: SKU stock counts, seeded for tests.
DM-6 Changes to orders (decided, to be reflected in design.md and schema):
- items JSONB column on orders. The inventory worker reads SKUs from it. Trade-off: no foreign-key integrity on SKUs.
- request_hash stored with the idempotency key. Same key and same hash returns the original response. Same key and a different hash returns 409.
- Idempotency keys are unique per (customer_id, idempotency_key), not globally.
6. API (order-api) [Y]
API-1 POST /orders body: customer_id, amount_cents, items. Header Idempotency-Key.
Writes order row and outbox row in ONE transaction.
Returns 201 with order id. Returns the original response (not a second order) when the same key is replayed.
Same key with a different body returns 409 (decide and document).
API-2 GET /orders/{id} returns status and saga step. 404 if unknown.
API-3 GET /health. [C]
API-4 Every request gets a correlation id, logged and propagated into events.
7. Outbox and relay [Y]
OBX-1 Order row and outbox row commit together or not at all (tests/test_outbox_atomicity.py, including the sabotage test).
OBX-2 Relay polls unpublished rows in id order, publishes to RabbitMQ, then sets published_at.
OBX-3 Relay crash after publish and before marking causes a re-publish. Accepted and documented as at-least-once.
OBX-4 Multiple relay instances must not corrupt state (decide how: row locking such as FOR UPDATE SKIP LOCKED, or a single instance; document the choice).
OBX-5 Publisher confirms enabled so a "published" row was actually accepted by the broker.
8. Consumers and idempotency [Y]
CON-1 Payment worker consumes OrderCreated, charges (simulated), emits PaymentSucceeded or PaymentFailed.
CON-2 Inventory worker consumes the next saga command, reserves stock, emits StockReserved or StockRejected.
CON-3 Dedupe check and the business write happen in the SAME database transaction as the processed_messages insert.
CON-4 Ack only after commit. Nack or leave unacked on failure.
CON-5 A manually redelivered message produces no second charge or reservation (tests/test_idempotency.py).
CON-6 Failure injection: each worker can fail on command, deterministically (header, env flag, or special customer id).
9. Saga and compensation [Y]
SAGA-1 Orchestrated: one component owns the flow and the state. (Your override is allowed, with written reasoning.)
SAGA-2 State machine documented in design.md BEFORE code, listing states, transitions, and which transitions write to Postgres and publish to the broker.
SAGA-3 State is persisted at every step, so a restart resumes correctly.
SAGA-4 Happy path: order created, payment succeeds, stock reserved, order confirmed.
SAGA-5 Inventory fails after payment succeeds: refund issued exactly once, order marked failed.
SAGA-6 Compensations (refund, release) are idempotent.
SAGA-7 Written answer in design.md: what happens if the compensation itself fails?
10. Observability (minimal) [B]
OBS-1 Structured JSON logs with order id and correlation id on every line.
OBS-2 You can reconstruct one order's full path from logs alone.
11. Tests
TST-1 Atomicity (exists, you make it pass). [Y]
TST-2 Idempotency at API and consumer. [Y]
TST-3 Compensation: inventory failure leads to one refund. [Y]
TST-4 Chaos (minimum one): kill a worker mid-saga, restart, assert no double charge (INV-2). [Y assertions, C harness]
TST-5 Fixtures and CI wiring. [C]
12. Deployment [C scripts, Y verifies]
DEP-1 Dockerfile per service, one compose file.
DEP-2 GitHub Actions: lint, tests against real Postgres and RabbitMQ, build images.
DEP-3 Live public URL on Render or Railway (VM optional). git push to main updates it.
DEP-4 Demo on the live URL: duplicate Idempotency-Key produces exactly one charge. Screenshot it.
13. Documentation deliverables
DOC-1 docs/design.md: invariants, state machine, failure catalog (at least one entry "handled badly"), decisions and rejected alternatives, future work. [Y]
DOC-2 docs/ai-log.md: one entry per session, including at least one time the AI was wrong or you overruled it. [Y]
DOC-3 README.md: architecture diagram, how to run, what was built vs deferred, honest cost and "what changes at 100x". [B]
14. Build order and definition of done
Scaffold, compose healthy, pytest fails for the right reason. [C then Y verifies]
design.md sections 1-2.
Outbox atomicity green (TST-1).
Relay publishes; consumer logs through the broker.
Workers plus idempotency green (TST-2).
Saga happy path, then compensation green (TST-3).
Deploy live (DEP-3, DEP-4).
Chaos test (TST-4), failure catalog, README.

Done means: INV-1 to INV-3 each have a passing test or a documented reason they don't, the live URL works, and you can explain every line in sections 6 to 9 without notes.