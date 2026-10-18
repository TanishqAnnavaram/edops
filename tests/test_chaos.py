"""TST-4 chaos: kill a worker mid-saga with a real process kill, restart it, and check the order
still finishes with exactly one charge (INV-2). Real processes, broker and Postgres.

The kill lands at the worst moment for INV-2: the charge has committed but the message is not
acked. The broker then redelivers it to the replacement worker.
"""

from fastapi.testclient import TestClient

from app.main import app
from tests.support import scalar, wait_until

TERMINAL_STEPS = {"CONFIRMED", "FAILED", "COMPENSATED"}


def test_killing_payment_worker_mid_saga_never_double_charges(procs):
    """INV-2: no double charge across a SIGKILL between commit and ack. INV-1: the order still
    reaches a terminal state after the restart."""
    procs.start("app.relay", ready="relay started")
    procs.start("app.workers.inventory", ready="inventory started")
    procs.start("app.orchestrator", ready="orchestrator started")
    doomed = procs.start(
        "app.workers.payment", ready="payment started", FAULT_HANG="payment:after_commit"
    )

    client = TestClient(app)
    order_id = client.post(
        "/orders",
        json={"customer_id": "c-chaos", "amount_cents": 1999,
              "items": [{"sku": "SKU-1", "quantity": 1}]},
        headers={"Idempotency-Key": "chaos-1"},
    ).json()["order_id"]

    doomed.wait_for_log("injected hang at payment:after_commit")
    assert scalar("SELECT count(*) FROM payments WHERE order_id = %s", (order_id,)) == 1
    doomed.kill()

    replacement = procs.start("app.workers.payment", ready="payment started")
    # The redelivery must be stopped by the dedupe, not by the UNIQUE constraint. Without this
    # check the test passes even with dedupe broken: the first worker's commit already moved
    # the saga on, and the constraint keeps the charge count at 1 while the redelivered
    # message loops in the background.
    replacement.wait_for_log("duplicate OrderCreated skipped")

    def terminal():
        order = client.get(f"/orders/{order_id}").json()
        return order if order["saga_step"] in TERMINAL_STEPS else None

    order = wait_until(terminal, 30, "order never finished after the worker was killed",
                       procs.dump)
    assert (order["status"], order["saga_step"]) == ("CONFIRMED", "CONFIRMED")
    assert (order["charges"], order["refunds"]) == (1, 0)
    assert "requeueing" not in replacement.logs()
