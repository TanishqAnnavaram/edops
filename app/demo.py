"""Local demo dashboard (docs/demo-ui.md). Off unless DEMO_UI=1.

These endpoints are unauthenticated, list every recent order and rewrite outbox rows, so they
must never be reachable on a public URL: they return 404 unless DEMO_UI=1, and the app refuses
to start with DEMO_UI=1 on a hosting platform.
"""

import json
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from app.db import get_conn

HERE = Path(__file__).resolve().parent
# Variables a hosting platform sets on every service. Their presence means "not a laptop".
HOSTING_MARKERS = ("RAILWAY_ENVIRONMENT", "RENDER")
RECENT_ORDERS = 10

router = APIRouter(prefix="/demo", include_in_schema=False)


def enabled() -> bool:
    return os.environ.get("DEMO_UI") == "1"


def assert_safe_environment() -> None:
    """Refuse to start if the demo endpoints would be exposed on a hosting platform."""
    if not enabled():
        return
    found = [name for name in HOSTING_MARKERS if os.environ.get(name)]
    if found:
        raise RuntimeError(
            f"DEMO_UI=1 with {', '.join(found)} set: the demo endpoints are unauthenticated "
            "and must not run on a hosting platform. Unset DEMO_UI."
        )


def require_enabled() -> None:
    if not enabled():
        raise HTTPException(status_code=404)


def db():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[psycopg.Connection, Depends(db)]
Enabled = Depends(require_enabled)


@router.get("", response_class=HTMLResponse, dependencies=[Enabled])
def page():
    return (HERE / "demo.html").read_text(encoding="utf-8")


@router.get("/api/script", dependencies=[Enabled])
def script():
    return json.loads((HERE / "demo_script.json").read_text(encoding="utf-8"))


@router.get("/api/state", dependencies=[Enabled])
def state(conn: Conn):
    orders = conn.execute(
        """
        SELECT o.id::text, o.customer_id, o.idempotency_key, o.status, s.step, o.created_at,
               (SELECT count(*) FROM payments p WHERE p.order_id = o.id AND p.kind = 'charge'),
               (SELECT count(*) FROM payments p WHERE p.order_id = o.id AND p.kind = 'refund')
        FROM orders o LEFT JOIN saga_state s ON s.order_id = o.id
        ORDER BY o.created_at DESC
        LIMIT %s
        """,
        (RECENT_ORDERS,),
    ).fetchall()
    stock = conn.execute("SELECT sku, stock FROM inventory ORDER BY sku").fetchall()
    keys = ("order_id", "customer_id", "idempotency_key", "status", "saga_step", "created_at",
            "charges", "refunds")
    return {
        "orders": [dict(zip(keys, row, strict=True)) for row in orders],
        "stock": [{"sku": sku, "stock": n} for sku, n in stock],
    }


@router.get("/api/orders/{order_id}/timeline", dependencies=[Enabled])
def timeline(order_id: UUID, conn: Conn):
    rows = conn.execute(
        """
        SELECT o.event_type, o.message_id::text, o.created_at, o.published_at,
               string_agg(p.consumer, ', ') AS processed_by
        FROM outbox o
        LEFT JOIN processed_messages p ON p.message_id = o.message_id
        WHERE o.aggregate_id = %s
        GROUP BY o.id
        ORDER BY o.id
        """,
        (str(order_id),),
    ).fetchall()
    keys = ("event_type", "message_id", "created_at", "published_at", "processed_by")
    return [dict(zip(keys, row, strict=True)) for row in rows]


@router.post("/api/orders/{order_id}/redeliver", dependencies=[Enabled])
def redeliver(order_id: UUID, conn: Conn):
    """Make the relay publish this order's OrderCreated again, same message_id: exactly what
    a broker redelivery looks like to the payment worker."""
    try:
        row = conn.execute(
            """
            UPDATE outbox SET published_at = NULL
            WHERE aggregate_id = %s AND event_type = 'OrderCreated'
            RETURNING message_id::text, now()
            """,
            (str(order_id),),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="no OrderCreated event for that order")
        # The outbox trigger only NOTIFYs on INSERT; this is an UPDATE, so wake the relay here
        # instead of waiting for its fallback poll. Delivered at commit, like the trigger's.
        conn.execute("SELECT pg_notify('outbox', '')")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    message_id, requested_at = row
    return {"message_id": message_id, "requested_at": requested_at}
