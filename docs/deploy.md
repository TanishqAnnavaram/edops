# Deploying edops to Railway

Seven Railway services in one project: Postgres, RabbitMQ, and five app processes built from
this repo's Dockerfile with different start commands. A push to `main` redeploys the app
services (DEP-3).

`.railway/railway.ts` describes the same setup as code, but it is an unverified draft (written
without an account). Either run `railway config plan` and fix what it rejects, or follow the
dashboard steps below, which do not depend on it.

## Services

| Service | Source | Start command | Notes |
|---|---|---|---|
| postgres | Railway Postgres template | (managed) | Exposes `DATABASE_URL` |
| rabbitmq | Docker image `rabbitmq:4.2.9-management` | (image default) | Volume at `/var/lib/rabbitmq`; no public domain |
| order-api | GitHub repo, Dockerfile | `python -m app.serve` | Pre-deploy `python -m app.init_db`; healthcheck `/health`; public domain |
| outbox-relay | GitHub repo, Dockerfile | `python -m app.relay` | No public domain |
| payment-worker | GitHub repo, Dockerfile | `python -m app.workers.payment` | No public domain |
| inventory-worker | GitHub repo, Dockerfile | `python -m app.workers.inventory` | No public domain |
| orchestrator | GitHub repo, Dockerfile | `python -m app.orchestrator` | No public domain |

## Environment variables

Generate one RabbitMQ password, letters and digits only (it goes inside a URL unescaped).

**rabbitmq**
| Name | Value |
|---|---|
| `RABBITMQ_DEFAULT_USER` | `edops` |
| `RABBITMQ_DEFAULT_PASS` | your generated password |
| `RABBITMQ_NODENAME` | `rabbit@localhost` (keeps the data directory stable across redeploys) |

**order-api, outbox-relay, payment-worker, inventory-worker, orchestrator** (all five)
| Name | Value |
|---|---|
| `DATABASE_URL` | `${{postgres.DATABASE_URL}}` (Railway reference variable) |
| `RABBITMQ_URL` | `amqp://edops:<password>@${{rabbitmq.RAILWAY_PRIVATE_DOMAIN}}:5672/` |

Railway sets `PORT` for order-api itself; `app.serve` reads it.

**Never set** `FAULT_CRASH`, `FAULT_HANG`, `EDOPS_BROKER_PREFIX`, `HEALTH_FILE` or
`TEST_DATABASE_URL` in any deployed service. The first two make processes exit or hang on
purpose; they exist only for tests.

The `fault-*` customer ids (`fault-payment-declined`, `fault-stock-rejected`,
`fault-after-reserve`) work on the live URL by design, as a way to demo failure paths.

## First deploy, then verify

1. Deploy postgres and rabbitmq first; wait until both are running.
2. Deploy order-api. Its pre-deploy step creates the schema and seeds inventory.
3. Deploy the four background services.
4. Run the smoke test from your machine:
   `python scripts/smoke.py https://<order-api public domain>`
   Expected last line: `RESULT: PASS, one order and exactly one charge`.

## Schema changes on a live database

`app.init_db` only creates missing objects (`IF NOT EXISTS`, `OR REPLACE`). A changed column or
CHECK on an existing table does not apply itself: there is no migration tool. Apply such a change
by hand with `railway connect postgres`, or reset the database if its data is disposable.
