"""Two simultaneous requests with the same key and body create one order (INV-2)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import app.orders as orders
from app.db import get_conn

ITEMS = [{"sku": "SKU-1", "quantity": 1}]


def count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def wait_for_lock_waiter(timeout: float = 5.0) -> bool:
    """True once another backend is blocked on a lock (the second INSERT on the unique index)."""
    probe = get_conn()
    # Autocommit: pg_stat_activity is snapshotted per transaction, so polling inside one
    # would never see the waiter appear.
    probe.autocommit = True
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            (waiters,) = probe.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            ).fetchone()
            if waiters:
                return True
            time.sleep(0.01)
        return False
    finally:
        probe.close()


def test_simultaneous_same_key_same_body_creates_one_order(conn, monkeypatch):
    real_insert_outbox = orders.insert_outbox
    saw_blocked_request = threading.Event()

    def insert_outbox_then_hold(*args, **kwargs):
        # Only the request that won the INSERT gets here. Hold its transaction open until the
        # other request is provably blocked on the same key, so the race is forced, not hoped for.
        real_insert_outbox(*args, **kwargs)
        if wait_for_lock_waiter():
            saw_blocked_request.set()

    monkeypatch.setattr(orders, "insert_outbox", insert_outbox_then_hold)
    start = threading.Barrier(2)

    def place_order():
        c = get_conn()
        try:
            start.wait(timeout=5)
            return orders.create_order(c, "cust-1", 1999, "key-race", items=ITEMS)
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(place_order) for _ in range(2)]
        first, second = (f.result(timeout=15) for f in futures)

    assert saw_blocked_request.is_set(), "second request never blocked; the race was not exercised"
    assert first == second
    assert count(conn, "orders") == 1
    assert count(conn, "outbox") == 1
