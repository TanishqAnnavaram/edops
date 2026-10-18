# Design: Event-Driven Order Processing

Write this yourself. Rough is fine. Nothing here ships without your own words.

## 1. Problem and invariants
An order passes through five processes (API, relay, payment worker, inventory worker,
orchestrator) connected by a broker that can redeliver, reorder or lose in-flight messages, and
any process can crash at any line. Three things must never happen:

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
  retries with the same Idempotency-Key. Every later step works the same way: the state change
  and the next event commit together, the relay publishes until the row is marked, and a
  consumer acks only after its commit, so an unfinished step is always redelivered.
- INV-2: Idempotency-Key at the API (unique per customer), processed_messages dedupe at each
  consumer, and `UNIQUE (order_id, kind)` on payments, so a second charge or a second refund is
  a constraint violation even if the code above it is wrong. A trigger also refuses a refund
  without a charge, or larger than it.
- INV-3: reservations carry a status; release is `UPDATE ... WHERE status = 'reserved'`, so a
  second release gives stock back zero times. Reservation is all-or-nothing (savepoint).

Where INV-1 is weaker than it sounds: "eventually" assumes every message can be processed. A
message whose handler always fails is requeued forever and its order never finishes. See the
failure catalog (F7, F10).

## 2. Order state machine
Two levels, owned by the orchestrator:
- **orders.status** (coarse, what a customer sees): `PENDING`, `CONFIRMED`, `FAILED`.
- **saga_state.step** (detail, one row per order, `order_id` is the PK): `PAYMENT_REQUESTED`,
  `STOCK_REQUESTED`, `RELEASE_REQUESTED`, `REFUND_REQUESTED`, `CONFIRMED`, `FAILED`,
  `COMPENSATED`.

Saga transitions (code: `TRANSITIONS` in `app/orchestrator.py`; T1 is in `app/orders.py`):

| # | Trigger | step: from -> to | orders.status | Writes Postgres | Publishes (outbox row) |
|---|---------|------------------|---------------|-----------------|------------------------|
| T1 | POST /orders (api) | (none) -> PAYMENT_REQUESTED | PENDING | yes | **yes: OrderCreated** |
| T2 | PaymentSucceeded | PAYMENT_REQUESTED -> STOCK_REQUESTED | PENDING | yes | **yes: ReserveStock** |
| T3 | PaymentFailed | PAYMENT_REQUESTED -> FAILED | FAILED | yes | no |
| T4 | StockReserved | STOCK_REQUESTED -> CONFIRMED | CONFIRMED | yes | no |
| T4f | StockReserved, post-reservation check fails | STOCK_REQUESTED -> RELEASE_REQUESTED | PENDING | yes | **yes: ReleaseStock** |
| T5 | StockRejected | STOCK_REQUESTED -> REFUND_REQUESTED | PENDING | yes | **yes: RefundPayment** |
| T7 | StockReleased | RELEASE_REQUESTED -> REFUND_REQUESTED | PENDING | yes | **yes: RefundPayment** |
| T6 | PaymentRefunded | REFUND_REQUESTED -> COMPENSATED | FAILED | yes | no |

Worker steps (they write business rows and reply; they never touch saga_state):

| Consumer | Event | Writes | Replies with |
|---|---|---|---|
| payment | OrderCreated | payments `charge` (unless declined) | PaymentSucceeded / PaymentFailed |
| payment | RefundPayment | payments `refund` (once per order) | PaymentRefunded |
| inventory | ReserveStock | inventory decrement + reservations, all or nothing | StockReserved / StockRejected |
| inventory | ReleaseStock | reservations -> released, inventory increment | StockReleased |

Spec CON-1 has the payment worker consume OrderCreated directly, so there is no separate
ChargePayment command: OrderCreated is the charge request, and T1 already puts the saga in
PAYMENT_REQUESTED.

Rules:
- Every transition is one transaction. "Publishes" means it inserts an outbox row in that same
  transaction; only the relay talks to the broker.
- Terminal transitions (T3, T4, T6) update orders.status and saga_state.step in the same
  transaction, so the two levels can never disagree.
- The orchestrator locks the saga row (`FOR UPDATE`) per event, so two events for one order
  cannot both advance it.
- An event that does not match the current step (duplicate with a new id, late, out of order)
  is a logged no-op: `ignored <event> in step <step>`. A redelivery with the same id is stopped
  earlier by processed_messages: `duplicate <event> skipped`.
- Compensation after a reservation is sequential (release, then refund), so saga_state never
  waits on two replies at once.
- When INV-3 matters: payment runs before reservation and reservation is all-or-nothing, so the
  only path that releases stock is T4f, a check that fails after stock is held. Here that check
  is injected (`fault-after-reserve`); in a real system it would be fraud review or similar.

### Delivery: outbox and relay (OBX-2 to OBX-5)
- The relay selects unpublished rows `ORDER BY id ... FOR UPDATE SKIP LOCKED`, publishes each with
  publisher confirms and `mandatory=True`, then sets `published_at` and commits.
- **Crash after publish, before mark** (OBX-3): the row stays unmarked and is published again
  with the same `message_id` (= `outbox.message_id`). Accepted: delivery is at-least-once and
  every consumer dedupes. Tested: `test_crash_between_publish_and_mark_republishes_exactly_once`.
- **Several relays** (OBX-4): SKIP LOCKED makes each row visible to one relay at a time; a
  relay that dies releases its locks with its connection. Cost: global id order holds per relay
  batch, not across relays. That is safe here because each order's next event is only written
  after its previous one was consumed, so one order never has two unpublished rows racing.
- **Confirms** (OBX-5): `published_at` is set only after the broker acknowledged the message;
  an unroutable or nacked publish raises and the row stays unmarked.
- The relay sleeps on `LISTEN outbox` (an insert trigger NOTIFYs at commit) with a 1 s timeout
  as a fallback, so it does not busy-poll.

### Failure injection (CON-6), all in `app/faults.py`
| Fault | How | Where it acts |
|---|---|---|
| payment declined | customer_id `fault-payment-declined` | payment worker -> PaymentFailed |
| stock rejected | customer_id `fault-stock-rejected` (or any short or unknown SKU) | inventory -> StockRejected |
| failure after reservation | customer_id `fault-after-reserve` | orchestrator T4f |
| crash | env `FAULT_CRASH=<process>:<point>`; points `before_commit`, `after_commit`, `relay:after_publish` | `os._exit(1)` at that line |
| hang | env `FAULT_HANG=<process>:after_commit` | blocks until killed (chaos test) |

The customer-id faults work in every environment, the live URL included, on purpose: they
demo the failure paths. The env-var faults are inert unless set, and no deploy config sets them.

## 3. Failure catalog
| # | Failure | What happens | How handled | Verdict |
|---|---------|--------------|-------------|---------|
| F1 | API dies between the order INSERT and the commit | Transaction rolls back; no order, no event; client gets no 201 | Client retries with the same key | handled well (sabotage + fresh-connection tests) |
| F2 | Same Idempotency-Key sent twice, even concurrently | Second request waits on the unique index, then replays | `ON CONFLICT DO NOTHING` + request_hash; 409 on a different body | handled well (race test) |
| F3 | Relay dies after the broker confirmed, before `published_at` | Row re-published with the same message_id | Consumers dedupe | handled well (accepted duplicate) |
| F4 | Two relays run at once | Rows split by SKIP LOCKED | No double send | handled well |
| F5 | Worker dies before commit | Nothing persisted, message unacked | Broker redelivers; processed fully | handled well |
| F6 | Worker dies after commit, before ack | Message redelivered | processed_messages row stops a second charge | handled well (chaos test) |
| F7 | A message whose handler always raises (bug, bad payload, DB constraint) | nack + requeue, redelivered immediately, forever | Nothing: no retry limit, no backoff, no DLQ. The order stays PENDING, the worker spins and floods logs, and with prefetch 1 that worker's queue is blocked behind it | **handled badly** |
| F8 | Outbox row whose event type no queue accepts | Relay raises, exits, restarts, hits the same row | Every later row waits behind it (head-of-line block) for all orders | **handled badly** |
| F9 | Orchestrator dies mid-transition | Step persisted or not, atomically; message redelivered | Resumes from saga_state | handled well (restart test) |
| F10 | Compensation fails (refund handler keeps raising) | Same as F7, at REFUND_REQUESTED: customer charged, order PENDING, no refund | Manual intervention only | **handled badly** |
| F11 | Duplicate or late event for a finished saga | Logged and ignored | Transition table has no entry | handled well |
| F12 | Postgres restarts or drops connections | API requests fail while it is down (one connection per request, so the next request reconnects); relay and workers see a broken connection and exit; the supervisor restarts them | Unacked messages return to the queue; nothing lost. Found during this build: before the fix, workers kept their dead connection and requeued one message about 1,200 times a second, forever, while reporting healthy | handled acceptably (`test_db_outage.py`); relies on a restart policy |
| F13 | Process alive but stuck (deadlock, hung call) | Heartbeat file goes stale; compose marks it unhealthy | Compose does not restart unhealthy containers; Railway has no exec healthcheck for workers | **handled badly**: detected, not fixed |
| F14 | RabbitMQ volume lost | Messages already marked published vanish | Their orders stay in the last step forever | **handled badly**: needs reconciliation |

### SAGA-7: what if the compensation itself fails?
Honestly: nothing automatic. A refund or release is a message like any other. If its handler
raises, the message is requeued forever (F7/F10) and the saga sits in REFUND_REQUESTED or
RELEASE_REQUESTED. Money is never moved twice (refund is unique per order and checked against
the charge), so the failure mode is "stuck and visible", not "wrong". Fixing it needs the
deferred pieces: bounded retries with backoff, a DLQ for messages that exhaust them, and a
reconciliation job that finds sagas stuck in a non-terminal step past a deadline and alerts or
re-drives them.

## 4. Decisions and rejected alternatives
- **Broker (RabbitMQ) vs Postgres-as-a-queue.** Chose RabbitMQ: the spec requires it, it pushes
  to consumers, routes by event type, and lets consumers scale independently. Honestly, at this
  volume a Postgres queue (`SKIP LOCKED` polling, which the relay already does) would work with
  one fewer system to run. Rejected for this build because the point is to handle a real
  broker's redelivery semantics; Postgres stays the source of truth either way.
- **Orchestrated vs choreographed saga.** Chose orchestration: the whole flow is one table in
  one file, the state is one row, and "what happens next" is answerable by reading it. Rejected
  choreography (each service reacts to the previous one's events): the flow becomes implicit,
  and compensation logic spreads across services. Cost: one more process, and the orchestrator
  is a single point every order passes through.
- **Orchestrator in its own process** (not in the relay or a worker). Keeps the relay
  publish-only and the workers business-only, so each crash test has one meaning, and the
  orchestrator can restart without stopping payments or publishing.
- **Delivery semantics: at-least-once.** Exactly-once across a broker and a database is not
  available without distributed transactions. At-most-once (ack before work) would drop
  orders on a crash (INV-1). At-least-once plus idempotent consumers gives effectively-once
  side effects.
- **Charge relies on the dedupe; refund and release are idempotent by state.** The charge is
  a plain INSERT, so a dedupe failure surfaces as a UNIQUE violation (loud) instead of being
  absorbed. Compensations check state first (SAGA-6), because a stuck compensation is worse
  than a re-answered one.
- **Refund-needs-charge as a trigger, not a foreign key.** An FK would need a `refund_of`
  column on refund rows; the trigger keeps the payments shape and also checks the amount.
- **Failing message: requeue forever vs drop.** Chose requeue (documented as F7). Dropping
  (`requeue=False` with no DLQ) loses the order outright, which breaks INV-1 silently.
- **Tests get their own database and broker namespace.** `edops_test` is rebuilt from
  init.sql each run, and each test uses a unique exchange/queue prefix, so tests can truncate
  and publish while the compose stack runs.
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
- **Hosting: Railway.** Runs all seven services in one project for roughly $10-15/month on
  the Hobby plan (estimate). Render needs a paid $7 instance per background worker plus hosted
  RabbitMQ, roughly $40-45/month.

## 5. Known future work
Deferred on purpose; none of this is implemented.
- **Retries with backoff and jitter**, bounded per message (fixes the spin in F7/F10).
- **DLQ and a replay CLI**: messages that exhaust retries go to a dead-letter queue; a CLI
  inspects and re-publishes them after a fix.
- **Reconciliation job**: finds sagas in a non-terminal step past a deadline (F10, F14), and
  outbox rows unpublished for too long (F8), and alerts or re-drives them.
- **Load test**: relay throughput (one confirm round-trip per message today), consumer lag.
- **More chaos tests**: kill the relay mid-batch, restart RabbitMQ with messages in flight,
  restart Postgres during a saga.
- Saga timeouts (a step waiting on a reply that never comes), a migration tool, quorum queues,
  a DB connection pool, restarting unhealthy processes (F13), and pruning `outbox` and
  `processed_messages`, which grow forever today.

## 6. Parking lot (ideas not in this build)
- Read the stored response for replays instead of rebuilding it (only matters once the POST
  response grows beyond `order_id`).
- Per-order ordering guarantees across several relays (partition rows by order id).

## 7. Invariant to test map
| Invariant | Tests that prove it |
|---|---|
| INV-1 No lost order | `test_outbox_atomicity.py` (all 4, incl. sabotage and fresh-connection commit), `test_relay.py::test_posted_order_reaches_a_consumer_through_the_broker`, `test_relay.py::test_crash_between_publish_and_mark_republishes_exactly_once`, `test_idempotency.py::test_crash_before_commit_persists_nothing_and_redelivery_succeeds`, `test_saga.py::test_happy_path_reaches_confirmed`, `test_saga.py::test_payment_failure_fails_the_order_without_charging`, `test_saga.py::test_orchestrator_restart_mid_saga_resumes`, `test_db_outage.py`, `test_chaos.py` |
| INV-2 No double charge | `test_idempotency.py::test_redelivered_order_created_charges_once`, `::test_crash_after_commit_before_ack_is_deduped_on_redelivery`, `::test_two_refund_commands_refund_once`, `::test_same_idempotency_key_twice_creates_one_order`, `test_idempotency_race.py`, `test_orders_api.py` replay and 409 tests, `test_schema.py::test_second_charge_for_same_order_is_rejected`, `test_payment_rules.py` (3), `test_saga.py::test_inventory_failure_after_payment_refunds_exactly_once` (2), `test_saga.py::test_duplicate_and_late_events_after_confirmation_are_ignored`, `test_chaos.py::test_killing_payment_worker_mid_saga_never_double_charges` |
| INV-3 No orphaned reservation | `test_saga.py::test_failure_after_reservation_releases_and_refunds_exactly_once`, `test_idempotency.py::test_two_release_commands_restore_stock_once`, `::test_reservation_is_all_or_nothing`, `::test_redelivered_reserve_stock_reserves_once` |

Gaps: none of the three lacks a passing test. INV-1's "eventually" is not proven for a message
that can never succeed (F7): that case is documented as handled badly, not tested as passing.
