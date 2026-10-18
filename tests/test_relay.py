"""Outbox relay against real Postgres and RabbitMQ (OBX-2 to OBX-5). Traces to INV-1."""

from fastapi.testclient import TestClient

from app.main import app
from app.orders import create_order, insert_outbox
from tests.support import scalar, wait_until

ITEMS = [{"sku": "SKU-1", "quantity": 1}]


def unpublished() -> int:
    return scalar("SELECT count(*) FROM outbox WHERE published_at IS NULL")


def test_posted_order_reaches_a_consumer_through_the_broker(procs):
    """INV-1: an accepted order's event leaves the database and is delivered."""
    audit = procs.start("app.audit", ready="audit started")
    procs.start("app.relay", ready="relay started")

    r = TestClient(app).post(
        "/orders",
        json={"customer_id": "c1", "amount_cents": 500, "items": ITEMS},
        headers={"Idempotency-Key": "k1", "X-Correlation-ID": "corr-relay"},
    )
    order_id = r.json()["order_id"]

    audit.wait_for_log(order_id)
    line = next(x for x in audit.json_lines() if x["order_id"] == order_id)
    assert line["msg"].startswith("saw OrderCreated")
    assert line["correlation_id"] == "corr-relay"
    wait_until(lambda: unpublished() == 0, 10, "outbox row never marked published")


def test_crash_between_publish_and_mark_republishes_exactly_once(procs, bus, conn):
    """OBX-3, INV-1: a relay that dies after the broker confirmed but before published_at
    committed leaves the row unmarked; the next relay publishes it again, once."""
    probe = bus.probe()
    order_id = create_order(conn, "c1", 500, "k1", items=ITEMS)

    crashing = procs.start("app.relay", FAULT_CRASH="relay:after_publish")
    assert crashing.wait_exit() == 1
    assert unpublished() == 1, "row must stay unmarked when the relay dies before marking"
    wait_until(lambda: bus.depth(probe) == 1, 10, "the first publish never reached the broker")

    procs.start("app.relay", ready="relay started")
    wait_until(lambda: unpublished() == 0, 10, "restarted relay never marked the row")

    # Every confirmed publish is already enqueued, so the probe now holds all deliveries.
    deliveries = bus.drain(probe)
    assert len(deliveries) == 2
    first, second = (props.message_id for props, _ in deliveries)
    assert first == second, "a re-publish must reuse outbox.message_id so consumers can dedupe"
    assert {payload["order_id"] for _, payload in deliveries} == {order_id}


def test_two_relays_publish_every_row_exactly_once(procs, bus, conn):
    """OBX-4: with FOR UPDATE SKIP LOCKED, concurrent relays split rows and never double-send."""
    probe = bus.probe()
    for _ in range(2):
        procs.start("app.relay", ready="relay started", RELAY_BATCH_SIZE="3")
    order_ids = {create_order(conn, f"c{i}", 500, f"k{i}", items=ITEMS) for i in range(40)}

    wait_until(lambda: unpublished() == 0, 20, "relays never drained the outbox", procs.dump)
    deliveries = bus.drain(probe)
    message_ids = [props.message_id for props, _ in deliveries]
    assert len(message_ids) == 40
    assert len(set(message_ids)) == 40
    assert {payload["order_id"] for _, payload in deliveries} == order_ids


def test_unroutable_event_is_not_marked_published(procs, conn):
    """OBX-5: mandatory publish plus confirms means a message no queue accepted is a failure,
    not a silent drop, so the row stays in the outbox."""
    order_id = create_order(conn, "c1", 500, "k1", items=ITEMS)
    with conn.cursor() as cur:
        cur.execute("UPDATE outbox SET published_at = now()")
        insert_outbox(cur, order_id, "NoSuchEvent", {"order_id": order_id})
    conn.commit()

    relay = procs.start("app.relay")
    assert relay.wait_exit() != 0
    assert "UnroutableError" in relay.logs()
    assert unpublished() == 1
