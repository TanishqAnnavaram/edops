"""Consumer and API idempotency (CON-3 to CON-6, TST-2) with real workers and a real broker."""

from fastapi.testclient import TestClient

from app.main import app
from app.orders import create_order
from tests.support import scalar, wait_until

ITEMS = [{"sku": "SKU-1", "quantity": 2}]


def outbox_count(event_type: str) -> int:
    return scalar("SELECT count(*) FROM outbox WHERE event_type = %s", (event_type,))


def payments(kind: str) -> int:
    return scalar("SELECT count(*) FROM payments WHERE kind = %s", (kind,))


def stock(sku: str) -> int:
    return scalar("SELECT stock FROM inventory WHERE sku = %s", (sku,))


def order_created(conn, customer_id: str = "c1") -> tuple[str, str, dict]:
    """Create an order and return (order_id, message_id, payload) of its OrderCreated row."""
    order_id = create_order(conn, customer_id, 1999, "k1", items=ITEMS)
    message_id, payload = conn.execute(
        "SELECT message_id::text, payload FROM outbox WHERE aggregate_id = %s", (order_id,)
    ).fetchone()
    conn.rollback()
    return order_id, message_id, payload


def test_redelivered_order_created_charges_once(procs, bus, conn):
    """INV-2: the same message delivered twice produces one charge and one PaymentSucceeded."""
    payment = procs.start("app.workers.payment", ready="payment started")
    _, message_id, payload = order_created(conn)

    bus.publish("OrderCreated", payload, message_id)
    bus.publish("OrderCreated", payload, message_id)

    payment.wait_for_log("duplicate OrderCreated skipped")
    assert payments("charge") == 1
    assert outbox_count("PaymentSucceeded") == 1
    assert scalar("SELECT count(*) FROM processed_messages") == 1


def test_redelivered_reserve_stock_reserves_once(procs, bus, conn):
    """INV-2/INV-3: a redelivered ReserveStock takes stock once and writes one reservation."""
    inventory = procs.start("app.workers.inventory", ready="inventory started")
    order_id, _, _ = order_created(conn)
    command = {"order_id": order_id, "customer_id": "c1", "items": ITEMS}

    message_id = bus.publish("ReserveStock", command)
    bus.publish("ReserveStock", command, message_id)

    inventory.wait_for_log("duplicate ReserveStock skipped")
    assert stock("SKU-1") == 8
    assert scalar("SELECT count(*) FROM reservations") == 1
    assert outbox_count("StockReserved") == 1


def test_same_idempotency_key_twice_creates_one_order(conn):
    """INV-2 at the API: a client retry with the same key cannot create a second order."""
    client = TestClient(app)
    body = {"customer_id": "c1", "amount_cents": 1999, "items": ITEMS}
    first = client.post("/orders", json=body, headers={"Idempotency-Key": "retry-1"})
    second = client.post("/orders", json=body, headers={"Idempotency-Key": "retry-1"})
    assert first.content == second.content
    assert scalar("SELECT count(*) FROM orders") == 1


def test_crash_after_commit_before_ack_is_deduped_on_redelivery(procs, bus, conn):
    """INV-2: the charge committed but the ack never happened, so the broker redelivers to the
    restarted worker; the dedupe row from the committed transaction stops a second charge."""
    _, message_id, payload = order_created(conn)
    crashing = procs.start("app.workers.payment", FAULT_CRASH="payment:after_commit")
    crashing.wait_for_log("payment started")

    bus.publish("OrderCreated", payload, message_id)
    assert crashing.wait_exit() == 1
    assert payments("charge") == 1

    restarted = procs.start("app.workers.payment", ready="payment started")
    restarted.wait_for_log("duplicate OrderCreated skipped")
    assert payments("charge") == 1
    assert outbox_count("PaymentSucceeded") == 1


def test_crash_before_commit_persists_nothing_and_redelivery_succeeds(procs, bus, conn):
    """INV-1: a crash inside the transaction rolls everything back, dedupe row included, so the
    redelivered message is processed in full rather than skipped."""
    _, message_id, payload = order_created(conn)
    crashing = procs.start("app.workers.payment", FAULT_CRASH="payment:before_commit")
    crashing.wait_for_log("payment started")

    bus.publish("OrderCreated", payload, message_id)
    assert crashing.wait_exit() == 1
    assert payments("charge") == 0
    assert scalar("SELECT count(*) FROM processed_messages") == 0
    assert outbox_count("PaymentSucceeded") == 0

    procs.start("app.workers.payment", ready="payment started")
    wait_until(lambda: outbox_count("PaymentSucceeded") == 1, 15, "redelivery never processed",
               procs.dump)
    assert payments("charge") == 1


def test_two_refund_commands_refund_once(procs, bus, conn):
    """INV-2 (SAGA-6): refund is idempotent by state; a second command moves no money."""
    order_id, _, _ = order_created(conn)
    conn.execute(
        "INSERT INTO payments (order_id, kind, amount_cents) VALUES (%s, 'charge', 1999)",
        (order_id,),
    )
    conn.commit()
    procs.start("app.workers.payment", ready="payment started")

    bus.publish("RefundPayment", {"order_id": order_id})
    bus.publish("RefundPayment", {"order_id": order_id})

    wait_until(lambda: outbox_count("PaymentRefunded") == 2, 15, "refunds never answered",
               procs.dump)
    assert payments("refund") == 1


def test_two_release_commands_restore_stock_once(procs, bus, conn):
    """INV-3 (SAGA-6): release flips reservation state, so stock comes back exactly once."""
    order_id, _, _ = order_created(conn)
    procs.start("app.workers.inventory", ready="inventory started")
    bus.publish("ReserveStock", {"order_id": order_id, "customer_id": "c1", "items": ITEMS})
    wait_until(lambda: stock("SKU-1") == 8, 15, "stock never reserved", procs.dump)

    bus.publish("ReleaseStock", {"order_id": order_id})
    bus.publish("ReleaseStock", {"order_id": order_id})

    wait_until(lambda: outbox_count("StockReleased") == 2, 15, "releases never answered",
               procs.dump)
    assert stock("SKU-1") == 10
    assert scalar("SELECT count(*) FROM reservations WHERE status = 'released'") == 1


def test_reservation_is_all_or_nothing(procs, bus, conn):
    """INV-3: when one SKU is short, stock already taken for other SKUs is put back."""
    order_id, _, _ = order_created(conn)
    procs.start("app.workers.inventory", ready="inventory started")
    items = [{"sku": "SKU-1", "quantity": 1}, {"sku": "SKU-EMPTY", "quantity": 1}]

    bus.publish("ReserveStock", {"order_id": order_id, "customer_id": "c1", "items": items})

    wait_until(lambda: outbox_count("StockRejected") == 1, 15, "never rejected", procs.dump)
    assert stock("SKU-1") == 10
    assert scalar("SELECT count(*) FROM reservations") == 0
