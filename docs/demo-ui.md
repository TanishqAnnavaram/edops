# Demoing edops with the mouse (UI walkthrough)

## Option 1: the demo dashboard (recommended)
One page with the whole script on it: **http://localhost:8000/demo**, after
`docker compose up -d --build --wait`.

- **Left column:** the demo script. Run steps 1 to 5 in order. Each has one button, the
  expected result, and your line to say. Steps 2 and 3 reuse step 1's idempotency key (shown
  under **Current request**), so they really are a retry and a conflicting retry.
- **Step 4** re-publishes step 1's OrderCreated with the same message id and shows the proof:
  "Re-published by the relay at …" next to "Charges: 1 before, 1 now".
- **Off this page:** a tick-list for the parts that happen elsewhere (stopping a worker in Docker
  Desktop, the chaos test in VS Code, CI). Ticks are remembered in the browser; **Clear ticks**
  resets them.
- **Right column** updates every second (it pauses while the tab is hidden): recent orders,
  the selected order's event timeline, stock, and the raw last response.

**Write your lines first.** Every "say" field in `app/demo_script.json` starts empty, and the page
shows a yellow reminder until you fill it in. Edit the file, then reload the page (the API
container needs `docker compose up -d --build` to pick up the change).

Each run of step 1 makes a fresh key, so no clean slate is needed between rehearsals.

The dashboard exists only because `docker-compose.yml` sets `DEMO_UI=1` on order-api. Without it
every `/demo` URL returns 404, and the API refuses to start if `DEMO_UI=1` is set on a hosting
platform.

## Option 2: the individual tools
The same demo as [local-demo.md](local-demo.md), done with browser tabs and desktop apps
instead of PowerShell. Every step here was checked against the running stack.

| Tool | Address | What it is for |
|---|---|---|
| **Docker Desktop** | the app | See the 7 services; stop, start and restart them; read their logs |
| **API docs (Swagger UI)** | http://localhost:8000/docs | Place orders and read them back |
| **Adminer** (database UI) | http://localhost:8080 | Look inside Postgres; run one SQL statement |
| **RabbitMQ dashboard** | http://localhost:15672 | See messages waiting in queues |
| **VS Code Testing panel** | flask icon in the left sidebar | Run the chaos test with a click |
| **GitHub Actions** | your repo, **Actions** tab | The green CI run |

---

## Before the demo (5 minutes)

### Start everything
The first start needs one command, because Docker Desktop cannot build images on its own.
Start **Docker Desktop**, wait until it says it is running, then in a terminal:
```powershell
cd "C:\Web dev\edops"
docker compose up -d --build --wait
```
After that, Docker Desktop shows a group called **edops** under **Containers**, and you can
start and stop the whole group with its ▶ / ■ buttons.

**Start every demo from a clean slate.** The steps below use the keys `demo-1` to `demo-4`, and
the database remembers keys, so a second run would hit old orders. This wipes the database
and resets stock:
```powershell
docker compose down -v
docker compose up -d --build --wait
```

### Open the tabs
1. **Docker Desktop → Containers → edops.** Expand it: 8 containers, all with a green dot
   (the 7 services plus Adminer).
2. **http://localhost:8000/docs**: the Swagger page listing `POST /orders`,
   `GET /orders/{order_id}` and `GET /health`.
3. **http://localhost:15672**: log in with user `edops`, password `edops`, then click the
   **Queues and Streams** tab. You should see `payment`, `inventory` and `orchestrator`.
4. **http://localhost:8080** (Adminer): fill in the login form like this:
   | Field | Value |
   |---|---|
   | System | **PostgreSQL** (the default is MySQL; change it) |
   | Server | `postgres` (already filled in) |
   | Username | `edops` |
   | Password | `edops` |
   | Database | `edops` |

   Tick **Permanent login** so it remembers you, then **Login**. You land on
   "Schema: public" with the 7 tables listed on the left.
5. **VS Code**: open the project folder, click the **flask icon** (Testing) in the left
   sidebar. If it asks for an interpreter, choose `C:\webdev\venvs\edops\Scripts\python.exe`.
   You should see 46 tests under `tests`.

Idempotency keys are remembered by the database. Reusing a key with the same body returns the
old order, and reusing it with a different body returns **409**, both by design. The SQL in the
steps below finds orders by their key (`demo-1`, `demo-2`, ...), so you never copy an order id
between tabs.

---

## Step 1: one order, sent twice (API idempotency)
In **Swagger** (http://localhost:8000/docs):
1. Click **POST /orders** → **Try it out**.
2. In **idempotency-key**, type `demo-1`.
3. Replace the request body with this, copied exactly:
   ```json
   {"customer_id": "c-demo", "amount_cents": 1999, "items": [{"sku": "SKU-1", "quantity": 1}]}
   ```
   SKUs are case-sensitive: the inventory has `SKU-1`, `SKU-2` and `SKU-EMPTY`. A typo such as
   `sku-1` is an unknown product, so the order is charged, rejected by inventory, and refunded
   (it ends `COMPENSATED`). That is correct behavior, just not this step's.
4. Click **Execute**. Response: code **201**, body `{"order_id": "..."}`. Copy the order id
   (only Swagger's GET step needs it).
5. Click **Execute** again, changing nothing. Same **201**, same `order_id`.
6. Optional: change `1999` to `5000` and Execute → **409** (same key, different body). Clear the
   idempotency-key box and Execute → **400** (key required). Put `1999` and `demo-1` back after.
7. Click **GET /orders/{order_id}** → **Try it out** → paste the id → **Execute**. The response
   shows `"status": "CONFIRMED"`, `"saga_step": "CONFIRMED"`, `"charges": 1`, `"refunds": 0`.

> *"A client retry with the same Idempotency-Key gets the original response back. It never
> creates a second order."*

## Step 2: the broker redelivers (consumer idempotency, the core question)
1. In **Adminer**, click **SQL command** (top of the left menu).
2. Paste this as is (it finds the Step 1 order by its key) and click **Execute**:
   ```sql
   UPDATE outbox SET published_at = NULL
   WHERE event_type = 'OrderCreated'
     AND aggregate_id = (SELECT id FROM orders
                         WHERE customer_id = 'c-demo' AND idempotency_key = 'demo-1')
   RETURNING message_id;
   ```
   Result: one row with a `message_id`, and "1 row affected". The relay now sends that same
   message again, which is exactly what a broker redelivery looks like to the payment worker.
3. In **Docker Desktop**, click the **payment-worker** container → **Logs** tab. Use the search
   box: type `duplicate`. You see
   `"msg": "duplicate OrderCreated skipped message_id=..."` with the id from step 2.
4. Back in **Swagger**, Execute **GET /orders/{order_id}** again: still `"charges": 1`.
5. Optional, in **Adminer → SQL command**:
   ```sql
   SELECT kind, amount_cents, created_at FROM payments
   WHERE order_id = (SELECT id FROM orders
                     WHERE customer_id = 'c-demo' AND idempotency_key = 'demo-1');
   ```
   One row, `charge`.

> *"The worker records the message id in the same database transaction as the charge, and acks
> only after commit. A redelivered message finds its id already recorded and is skipped. Under
> that, a UNIQUE constraint makes a second charge impossible at the database level."*

## Step 3: a failure after stock was reserved (compensation)
1. In **Swagger**, **POST /orders** with idempotency-key `demo-2` and this body:
   ```json
   {"customer_id": "fault-after-reserve", "amount_cents": 1999, "items": [{"sku": "SKU-2", "quantity": 1}]}
   ```
2. **GET /orders/{order_id}** with the new id: `"status": "FAILED"`,
   `"saga_step": "COMPENSATED"`, `"charges": 1`, `"refunds": 1`.
3. In **Adminer**'s left menu, click **reservations**, then **Select data** at the top of that
   page: the row for this order has `status` = `released`. Do the same for **inventory**:
   SKU-2's stock is back where it was before the order. (The small symbol just left of each
   table name in the menu is a shortcut for Select data.)

> *"The orchestrator owns the flow: payment, then stock. When something fails after both, it
> releases the stock, then refunds, each exactly once."*

Other customer ids that trigger failures: `fault-payment-declined` (FAILED, no charge) and
`fault-stock-rejected` (COMPENSATED, one charge and one refund).

## Step 4: the order's path through the system
In **Adminer → SQL command**, for the Step 1 order:
```sql
SELECT o.event_type, o.created_at, o.published_at,
       string_agg(p.consumer, ', ') AS processed_by
FROM outbox o
LEFT JOIN processed_messages p ON p.message_id = o.message_id
WHERE o.aggregate_id = (SELECT id FROM orders
                        WHERE customer_id = 'c-demo' AND idempotency_key = 'demo-1')
GROUP BY o.id
ORDER BY o.id;
```
For the compensated order from Step 3, change the last line of the inner query to
`WHERE customer_id = 'fault-after-reserve' AND idempotency_key = 'demo-2')`: you see the
longer path, ending in `ReleaseStock`, `StockReleased`, `RefundPayment`, `PaymentRefunded`.
Result: four rows in order, `OrderCreated` → `PaymentSucceeded` → `ReserveStock` →
`StockReserved`, each with when it was written, when it was published, and which process
consumed it (`payment`, `orchestrator`, `inventory`, `orchestrator`).

To see the same order in the logs, search for the order id in **Docker Desktop → Logs** of each
container in turn. (Docker Desktop shows one container at a time; the single merged log view
needs the PowerShell command in [local-demo.md](local-demo.md), Step 4.)

> *"Every step writes its next event in the same transaction as its state change. This table is
> the order's whole history, and each event shows who processed it."*

## Step 5: a worker goes down, and nothing is lost
1. **Docker Desktop**: on the **payment-worker** row, click **■ Stop**.
2. **Swagger**: POST a new order (idempotency-key `demo-3`, the body from Step 1).
3. **GET** it: `"status": "PENDING"`, `"saga_step": "PAYMENT_REQUESTED"`, `"charges": 0`.
4. **RabbitMQ → Queues and Streams**: the `payment` queue shows **1** message ready, waiting
   for a worker.
5. **Docker Desktop**: click **▶ Start** on payment-worker.
6. A few seconds later, **GET** again: `"CONFIRMED"`, `"charges": 1`. The `payment` queue
   is back to 0.

> *"The event was saved with the order, so a dead worker only delays it. The broker keeps the
> message until a worker acknowledges it after committing."*

## Step 6: restart the database under the running system
1. **Docker Desktop**: on the **postgres** row, click **⟳ Restart**. Wait for its dot to go
   green again (a few seconds).
2. **Swagger**: POST a new order (idempotency-key `demo-4`). If you get a **500** because the
   database is still coming up, click **Execute** again with the same key: that is the retry the
   key makes safe.
3. **GET** it: `"CONFIRMED"`, `"charges": 1`.
4. **Docker Desktop → payment-worker → Logs**, search `connection lost`: you see
   `database connection lost, exiting`, then a fresh `payment started`.

> *"A worker whose database connection dies exits on purpose, and Docker restarts it. Before
> that fix, workers retried one message about 1,200 times a second forever while reporting
> healthy. I found it while writing the failure catalog."*

## Step 7: kill a worker at the worst moment (chaos test)
In **VS Code → Testing** (flask icon), expand `tests` → `test_chaos.py` and click **▶** next to
`test_killing_payment_worker_mid_saga_never_double_charges`. A green tick appears after about
6 seconds. To run everything, click **▶** at the top of the panel: 46 green ticks in about a
minute.

> *"This test starts its own copy of every process, freezes the payment worker right after it
> committed the charge but before it acked, kills it, and starts a new one. The order still
> finishes with exactly one charge. It was shown failing against a deliberately broken dedupe,
> so it can actually catch the bug."*

## Step 8: the evidence
- **GitHub → Actions**: the green `ci` run. The same 46 tests, chaos included, on a clean Linux
  machine with real Postgres and RabbitMQ.
- **docs/design.md**: section 2 (state machine), section 3 (failure catalog; point at the
  entries marked **handled badly**), section 7 (which test proves each invariant).

> *"What it does not handle, on purpose: a message that always fails is retried forever, with no
> backoff and no dead-letter queue. That is documented as handled badly, and it is the first item
> of future work."*

---

## If something looks wrong
| Symptom | Fix |
|---|---|
| Adminer says it cannot connect | **System** must be **PostgreSQL**, server `postgres` |
| Swagger returns **400** | The idempotency-key box is empty |
| Swagger returns **409** | That key was already used with a different body; use a new key, or start over from a clean slate |
| Step 1's order ends `COMPENSATED` with 1 charge and 1 refund | The SKU was mistyped (they are case-sensitive: `SKU-1`, not `sku-1`), so inventory rejected it and the payment was refunded. Correct behavior; start over from a clean slate with the exact body |
| Adminer: `invalid input syntax for type uuid` | A placeholder or a partial id was pasted where an order id belongs. The queries in this guide look orders up by key, so paste them unchanged |
| Adminer query returns 0 rows | The key in the query does not match an order: check the key and customer id you used in Swagger |
| An order stays `PENDING` | Docker Desktop: is every container green? Then check that container's **Logs** |
| The Testing panel shows no tests | Select the interpreter `C:\webdev\venvs\edops\Scripts\python.exe` (Ctrl+Shift+P → **Python: Select Interpreter**), then click the refresh icon in the Testing panel |
| A test fails with "Postgres unreachable" | The stack is not running; start the **edops** group in Docker Desktop |
| `ModuleNotFoundError: No module named 'app'` when running a test | The file was run with plain Python (VS Code's **▷ Run Python File** button, top right of the editor). Tests must run through pytest: use the Testing panel, the green ▷ next to the test function, or `pytest tests/test_chaos.py -v` |

More fixes: [local-demo.md, Troubleshooting](local-demo.md#6-troubleshooting).
