"""Payment rules enforced by the database itself, below any application code (INV-2)."""

import psycopg
import pytest

from app.orders import create_order


def insert_payment(conn, order_id: str, kind: str, amount_cents: int = 1999) -> None:
    conn.execute(
        "INSERT INTO payments (order_id, kind, amount_cents) VALUES (%s, %s, %s)",
        (order_id, kind, amount_cents),
    )


def test_refund_without_a_charge_is_rejected(conn):
    """The trigger refuses a refund for an order that was never charged."""
    order_id = create_order(conn, "c1", 1999, "k1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="has no charge"):
        insert_payment(conn, order_id, "refund")
    conn.rollback()


def test_refund_larger_than_the_charge_is_rejected(conn):
    order_id = create_order(conn, "c1", 1999, "k1")
    insert_payment(conn, order_id, "charge", 1999)
    with pytest.raises(psycopg.errors.CheckViolation, match="exceeds charge"):
        insert_payment(conn, order_id, "refund", 2000)
    conn.rollback()


def test_second_refund_for_the_same_order_is_rejected(conn):
    """INV-2: money can come back once; UNIQUE (order_id, kind) blocks a second refund row."""
    order_id = create_order(conn, "c1", 1999, "k1")
    insert_payment(conn, order_id, "charge")
    insert_payment(conn, order_id, "refund")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_payment(conn, order_id, "refund")
    conn.rollback()
