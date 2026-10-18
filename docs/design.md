# Design: Event-Driven Order Processing

Write this yourself. Rough is fine. Nothing here ships without your own words.

## 1. Problem and invariants
- What must never happen? (aim for three sentences: no lost order, no double charge, no orphaned reservation)

## 2. Order state machine
- Statuses:
- Transitions:
- Mark every transition that writes to Postgres AND publishes to RabbitMQ.

## 3. Failure catalog
| # | Failure | What happens | How handled | Verdict |
|---|---------|--------------|-------------|---------|
At least one entry should end with "handled badly".

## 4. Decisions and rejected alternatives
- Broker (RabbitMQ) vs Postgres-as-a-queue: choice, reason, what I rejected.
- Orchestrated vs choreographed saga: choice, reason, what I rejected.
- Delivery semantics (at-least-once) and why:

## 5. Known future work
- Retries and DLQ, replay CLI, load test, reconciliation job.

## 6. Parking lot (ideas not in this build)
