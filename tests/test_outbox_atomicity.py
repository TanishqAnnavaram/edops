"""Your spec for the outbox. These FAIL until you write app/orders.py.

Contract to implement:
    create_order(conn, customer_id: str, amount_cents: int,
                 idempotency_key: str | None = None) -> str   # returns order id

Rule: the orders row and its outbox row are committed together or not at all.
"""
import pytest

from app.orders import create_order  # you create this module


def count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def test_order_and_outbox_written_together(conn):
    order_id = create_order(conn, "cust-1", 1999, "key-1")
    assert count(conn, "orders") == 1
    assert count(conn, "outbox") == 1
    with conn.cursor() as cur:
        cur.execute("SELECT aggregate_id::text FROM outbox")
        assert cur.fetchone()[0] == order_id


def test_successful_order_is_committed(conn):
    """A fresh connection only sees committed rows. If create_order forgets
    to commit, the rows above are visible on `conn` but lost on close (INV-1)."""
    create_order(conn, "cust-1", 1999, "key-4")

    from app.db import get_conn

    fresh = get_conn()
    try:
        assert count(fresh, "orders") == 1
        assert count(fresh, "outbox") == 1
    finally:
        fresh.close()


def test_outbox_row_starts_unpublished(conn):
    create_order(conn, "cust-1", 1999, "key-2")
    with conn.cursor() as cur:
        cur.execute("SELECT published_at FROM outbox")
        assert cur.fetchone()[0] is None


def test_sabotage_failure_after_order_insert_leaves_nothing(conn, monkeypatch):
    """Force a crash between the two inserts. Neither row may survive.

    Hint: make your outbox-insert step a separate function (e.g.
    app.orders.insert_outbox) so this patch has something to break.
    If it passes while the order row is still there, your transaction
    boundary is wrong. That is the point of this test.
    """
    import app.orders as orders

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(orders, "insert_outbox", boom)
    with pytest.raises(RuntimeError):
        create_order(conn, "cust-1", 1999, "key-3")

    fresh = __import__("app.db", fromlist=["get_conn"]).get_conn()
    try:
        assert count(fresh, "orders") == 0
        assert count(fresh, "outbox") == 0
    finally:
        fresh.close()
