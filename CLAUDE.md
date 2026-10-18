# edops: Event-Driven Order Processing

Small order system whose failure behavior is understood and provable.
Success test: explain what happens if the broker redelivers a message
after a consumer crash, and prove no card was charged twice.
Full spec: docs/spec.md. Design decisions: docs/design.md.

## Stack
Python 3.12, FastAPI, PostgreSQL 16, RabbitMQ, Docker Compose, GitHub Actions.
Postgres is the source of truth. RabbitMQ carries events only. No Redis.
Delivery is at-least-once; consumers must be idempotent.

## Commands
- Start services: docker compose up -d
- Check health: docker compose ps
- Tests: pytest
- API: uvicorn app.main:app --reload --port 8000
- Venv lives outside the repo.

## Invariants (every test traces to one)
- INV-1 No lost order
- INV-2 No double charge
- INV-3 No orphaned reservation

## Working rules
- Work on one step at a time. Do only what the prompt names.
- Start in plan mode: list assumptions and files you will touch, then wait for approval.
- Never edit or weaken a test to make it pass. If a test seems wrong, say so and stop.
- Never swallow exceptions. Every DB write that must be atomic uses one transaction.
- No new dependencies, renames or refactors without asking.
- Before coding, explain in 2-3 sentences the failure this step prevents.
- After coding, list the 3 riskiest lines and what breaks if each is wrong.
- Use structured JSON logs with order id and correlation id on every line.
- Whenever a table is added to db/init.sql, add it to the TRUNCATE in tests/conftest.py.

## Done means
Tests pass, I ran them myself, and the user's verify gate for the step is complete.
Do not commit. The user commits.