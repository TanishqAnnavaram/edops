"""Database-level guarantees that hold even if application code is wrong."""
import psycopg
import pytest

from app.orders import create_order


def test_second_charge_for_same_order_is_rejected(conn):
    """INV-2 at the last line of defence: the payments table itself refuses a double charge."""
    order_id = create_order(conn, "cust-1", 1999, "key-1")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO payments (order_id, kind, amount_cents) VALUES (%s, 'charge', 1999)",
            (order_id,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO payments (order_id, kind, amount_cents) VALUES (%s, 'charge', 1999)",
                (order_id,),
            )
    conn.rollback()


def test_one_charge_and_one_refund_are_allowed(conn):
    order_id = create_order(conn, "cust-1", 1999, "key-1")
    with conn.cursor() as cur:
        for kind in ("charge", "refund"):
            cur.execute(
                "INSERT INTO payments (order_id, kind, amount_cents) VALUES (%s, %s, 1999)",
                (order_id, kind),
            )
    conn.commit()
