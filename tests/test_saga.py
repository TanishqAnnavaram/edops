"""Orchestrated saga end to end (SAGA-3 to SAGA-6, TST-3): API in-process, relay, both workers
and the orchestrator as real processes, real broker, real Postgres."""

import logging

import pytest
from fastapi.testclient import TestClient

from app import faults
from app.main import app
from tests.support import scalar, wait_until

ITEMS = [{"sku": "SKU-1", "quantity": 2}]
TERMINAL_STEPS = {"CONFIRMED", "FAILED", "COMPENSATED"}
client = TestClient(app)


def start_pipeline(procs, **orchestrator_env):
    procs.start("app.relay", ready="relay started")
    procs.start("app.workers.payment", ready="payment started")
    procs.start("app.workers.inventory", ready="inventory started")
    return procs.start("app.orchestrator", ready="orchestrator started", **orchestrator_env)


def place_order(customer_id="c1", items=ITEMS, correlation_id="corr-saga") -> str:
    r = client.post(
        "/orders",
        json={"customer_id": customer_id, "amount_cents": 1999, "items": items},
        headers={"Idempotency-Key": f"key-{customer_id}", "X-Correlation-ID": correlation_id},
    )
    assert r.status_code == 201, r.text
    return r.json()["order_id"]


def get(order_id: str) -> dict:
    return client.get(f"/orders/{order_id}").json()


def wait_terminal(order_id: str, procs, timeout: float = 30) -> dict:
    def terminal():
        order = get(order_id)
        return order if order["saga_step"] in TERMINAL_STEPS else None

    return wait_until(
        terminal,
        timeout,
        f"order {order_id} never reached a terminal step (last: {get(order_id)['saga_step']})",
        procs.dump,
    )


def stock(sku: str) -> int:
    return scalar("SELECT stock FROM inventory WHERE sku = %s", (sku,))


def outbox_count(event_type: str) -> int:
    return scalar("SELECT count(*) FROM outbox WHERE event_type = %s", (event_type,))


def test_happy_path_reaches_confirmed(procs):
    """INV-1: an accepted order is processed to a terminal state: CONFIRMED, charged once,
    stock reserved."""
    start_pipeline(procs)
    order_id = place_order()

    order = wait_terminal(order_id, procs)

    assert (order["status"], order["saga_step"]) == ("CONFIRMED", "CONFIRMED")
    assert (order["charges"], order["refunds"]) == (1, 0)
    assert stock("SKU-1") == 8
    assert scalar("SELECT count(*) FROM reservations WHERE status = 'reserved'") == 1


def test_payment_failure_fails_the_order_without_charging(procs):
    """INV-1: a declined payment is an explicit FAILED, not a stuck order."""
    start_pipeline(procs)
    order_id = place_order(customer_id=faults.PAYMENT_DECLINED)

    order = wait_terminal(order_id, procs)

    assert (order["status"], order["saga_step"]) == ("FAILED", "FAILED")
    assert (order["charges"], order["refunds"]) == (0, 0)
    assert outbox_count("ReserveStock") == 0


@pytest.mark.parametrize(
    "customer_id, items",
    [
        (faults.STOCK_REJECTED, ITEMS),
        ("c-short", [{"sku": "SKU-EMPTY", "quantity": 1}]),
    ],
    ids=["injected", "out-of-stock"],
)
def test_inventory_failure_after_payment_refunds_exactly_once(procs, customer_id, items):
    """INV-2: payment succeeded, inventory rejected; the charge is refunded exactly once, so
    the net charge is zero, and the order is FAILED."""
    start_pipeline(procs)
    order_id = place_order(customer_id=customer_id, items=items)

    order = wait_terminal(order_id, procs)

    assert (order["status"], order["saga_step"]) == ("FAILED", "COMPENSATED")
    assert (order["charges"], order["refunds"]) == (1, 1)
    assert scalar("SELECT count(*) FROM reservations") == 0


def test_failure_after_reservation_releases_and_refunds_exactly_once(procs):
    """INV-3: stock reserved for an order that then fails is released exactly once, and the
    payment is refunded exactly once."""
    start_pipeline(procs)
    order_id = place_order(customer_id=faults.FAIL_AFTER_RESERVE)

    order = wait_terminal(order_id, procs)

    assert (order["status"], order["saga_step"]) == ("FAILED", "COMPENSATED")
    assert (order["charges"], order["refunds"]) == (1, 1)
    assert stock("SKU-1") == 10
    assert scalar("SELECT count(*) FROM reservations WHERE status = 'released'") == 1
    assert outbox_count("ReleaseStock") == 1


def test_orchestrator_restart_mid_saga_resumes(procs):
    """INV-1 (SAGA-3): the orchestrator commits PaymentSucceeded -> STOCK_REQUESTED and dies
    before acking. The restarted one sees the redelivery as a duplicate, resumes from the
    persisted step, and the saga still finishes with exactly one ReserveStock command."""
    crashing = start_pipeline(procs, FAULT_CRASH="orchestrator:after_commit")
    order_id = place_order()

    assert crashing.wait_exit(20) == 1
    assert get(order_id)["saga_step"] == "STOCK_REQUESTED"

    restarted = procs.start("app.orchestrator", ready="orchestrator started")
    order = wait_terminal(order_id, procs)

    assert order["status"] == "CONFIRMED"
    restarted.wait_for_log("duplicate PaymentSucceeded skipped")
    assert outbox_count("ReserveStock") == 1
    assert order["charges"] == 1


def test_duplicate_and_late_events_after_confirmation_are_ignored(procs, bus):
    """INV-2/INV-3: a redelivered PaymentSucceeded and a late StockRejected for a finished saga
    change nothing: no second reservation, no refund, status stays CONFIRMED."""
    orchestrator = start_pipeline(procs)
    order_id = place_order()
    wait_terminal(order_id, procs)
    message_id, payload = (
        scalar(
            "SELECT json_build_array(message_id::text, payload) FROM outbox "
            "WHERE event_type = 'PaymentSucceeded'"
        )
    )

    bus.publish("PaymentSucceeded", payload, message_id)
    bus.publish("StockRejected", {"order_id": order_id, "correlation_id": "corr-late"})

    orchestrator.wait_for_log("duplicate PaymentSucceeded skipped")
    orchestrator.wait_for_log("ignored StockRejected in step CONFIRMED")
    order = get(order_id)
    assert (order["status"], order["saga_step"]) == ("CONFIRMED", "CONFIRMED")
    assert (order["charges"], order["refunds"]) == (1, 0)
    assert outbox_count("ReserveStock") == 1
    assert outbox_count("RefundPayment") == 0


def test_one_orders_full_path_is_rebuilt_from_logs_alone(procs, caplog):
    """OBS-2: filtering every process's JSON log by order_id yields the whole path, from the
    API through relay, payment, orchestrator and inventory, under one correlation id."""
    caplog.set_level(logging.INFO)
    start_pipeline(procs)
    order_id = place_order(correlation_id="corr-trace")
    wait_terminal(order_id, procs)

    api_messages = [
        r.getMessage() for r in caplog.records if getattr(r, "order_id", None) == order_id
    ]
    process_lines = [line for line in procs.all_json_lines() if line["order_id"] == order_id]

    assert "order created" in api_messages
    assert {line["logger"] for line in process_lines} == {
        "app.relay", "app.payment", "app.inventory", "app.orchestrator",
    }
    assert {line["correlation_id"] for line in process_lines} == {"corr-trace"}
    transitions = [
        line["msg"] for line in process_lines
        if line["logger"] == "app.orchestrator" and "->" in line["msg"]
    ]
    assert transitions == [
        "PAYMENT_REQUESTED -> STOCK_REQUESTED on PaymentSucceeded",
        "STOCK_REQUESTED -> CONFIRMED on StockReserved",
    ]
