import hashlib
import json
import logging
import uuid

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)


class IdempotencyConflict(Exception):
    """Same (customer_id, idempotency_key) was used with a different request body."""


def request_hash(customer_id: str, amount_cents: int, items: list) -> str:
    # Canonical JSON so key order in the client's body cannot change the hash.
    canonical = json.dumps(
        {"customer_id": customer_id, "amount_cents": amount_cents, "items": items},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_order(
    conn: psycopg.Connection,
    customer_id: str,
    amount_cents: int,
    idempotency_key: str | None = None,
    *,
    items: list | tuple = (),
    correlation_id: str | None = None,
) -> str:
    """Write the order, its saga row and its OrderCreated outbox row in one transaction.

    A replay of the same key and body returns the existing order id and writes nothing.
    """
    items = list(items)
    req_hash = (
        request_hash(customer_id, amount_cents, items) if idempotency_key is not None else None
    )
    order_id = str(uuid.uuid4())
    try:
        with conn.cursor() as cur:
            # ON CONFLICT instead of SELECT-then-INSERT: a concurrent request with the same key
            # blocks on the unique index until the first commits, then takes the replay path.
            cur.execute(
                """
                INSERT INTO orders
                    (id, customer_id, idempotency_key, request_hash, amount_cents, items, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                ON CONFLICT (customer_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (order_id, customer_id, idempotency_key, req_hash, amount_cents, Jsonb(items)),
            )
            if cur.fetchone() is None:
                existing_id = _replayed_order_id(cur, customer_id, idempotency_key, req_hash)
                conn.rollback()
                log.info("idempotent replay", extra={"order_id": existing_id})
                return existing_id

            cur.execute(
                "INSERT INTO saga_state (order_id, step) VALUES (%s, 'PAYMENT_REQUESTED')",
                (order_id,),
            )
            insert_outbox(
                cur,
                order_id,
                "OrderCreated",
                {
                    "order_id": order_id,
                    "customer_id": customer_id,
                    "amount_cents": amount_cents,
                    "items": items,
                    "correlation_id": correlation_id,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log.info("order created", extra={"order_id": order_id})
    return order_id


def _replayed_order_id(cur, customer_id: str, idempotency_key: str, req_hash: str) -> str:
    cur.execute(
        "SELECT id::text, request_hash FROM orders WHERE customer_id = %s AND idempotency_key = %s",
        (customer_id, idempotency_key),
    )
    existing_id, existing_hash = cur.fetchone()
    if existing_hash != req_hash:
        log.warning("idempotency key reused with a different body", extra={"order_id": existing_id})
        raise IdempotencyConflict(existing_id)
    return existing_id


# Separate so the sabotage test can patch a failure between the two inserts.
def insert_outbox(cur, aggregate_id: str, event_type: str, payload: dict) -> None:
    cur.execute(
        """
        INSERT INTO outbox (message_id, aggregate_id, event_type, payload)
        VALUES (%s, %s, %s, %s)
        """,
        (str(uuid.uuid4()), aggregate_id, event_type, Jsonb(payload)),
    )


def get_order(conn: psycopg.Connection, order_id: str) -> dict | None:
    with conn.cursor() as cur:
        # LEFT JOIN so an order missing its saga row shows saga_step null instead of a 404.
        cur.execute(
            """
            SELECT o.id::text, o.customer_id, o.amount_cents, o.items, o.status, s.step
            FROM orders o LEFT JOIN saga_state s ON s.order_id = o.id
            WHERE o.id = %s
            """,
            (order_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("order_id", "customer_id", "amount_cents", "items", "status", "saga_step")
    return dict(zip(keys, row, strict=True))
