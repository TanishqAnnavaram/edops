"""A worker whose database connection dies must not spin forever (failure catalog F12)."""

from app.orders import create_order
from tests.support import scalar, wait_until


def terminate_other_backends() -> None:
    """What a Postgres restart does to every client: their connections are cut."""
    scalar(
        "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
    )


def test_worker_exits_on_lost_db_connection_and_a_restart_processes_the_message(
    procs, bus, conn
):
    """INV-1: after a database outage the message is not lost and not spun on forever: the
    worker exits, and a restarted worker charges once."""
    order_id = create_order(conn, "c1", 1999, "k1", items=[{"sku": "SKU-1", "quantity": 1}])
    message_id, payload = conn.execute(
        "SELECT message_id::text, payload FROM outbox WHERE aggregate_id = %s", (order_id,)
    ).fetchone()
    conn.rollback()
    worker = procs.start("app.workers.payment", ready="payment started")

    terminate_other_backends()
    bus.publish("OrderCreated", payload, message_id)

    assert worker.wait_exit() != 0
    assert "database connection lost, exiting" in worker.logs()
    assert worker.logs().count("requeueing") == 0

    procs.start("app.workers.payment", ready="payment started")
    wait_until(
        lambda: scalar("SELECT count(*) FROM payments WHERE kind = 'charge'") == 1,
        15,
        "restarted worker never processed the redelivered message",
        procs.dump,
    )
