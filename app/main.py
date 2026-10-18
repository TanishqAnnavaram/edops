import logging
import time
import uuid
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import get_conn
from app.log import configure_logging, correlation_id_var
from app.orders import IdempotencyConflict, create_order, get_order

configure_logging()
log = logging.getLogger("app.api")

app = FastAPI(title="edops order-api")


@app.middleware("http")
async def correlation_id(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    token = correlation_id_var.set(cid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        log.info(
            "%s %s %d %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - start) * 1000,
            # Endpoints run in a threadpool, so a contextvar set there never reaches this
            # frame; request.state lives in the shared ASGI scope and does.
            extra={"order_id": getattr(request.state, "order_id", None)},
        )
        return response
    finally:
        correlation_id_var.reset(token)


def db() -> Iterator[psycopg.Connection]:
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


class Item(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(gt=0)


class OrderIn(BaseModel):
    customer_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    items: list[Item] = Field(min_length=1)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/orders", status_code=201)
def post_order(
    request: Request,
    body: OrderIn,
    conn: Annotated[psycopg.Connection, Depends(db)],
    # Optional at the framework level so a missing key is our 400, not FastAPI's 422.
    idempotency_key: Annotated[str | None, Header()] = None,
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    try:
        order_id = create_order(
            conn,
            body.customer_id,
            body.amount_cents,
            idempotency_key,
            items=[item.model_dump() for item in body.items],
            correlation_id=correlation_id_var.get(),
        )
    except IdempotencyConflict as e:
        raise HTTPException(
            status_code=409, detail="Idempotency-Key was already used with a different body"
        ) from e
    request.state.order_id = order_id
    # Same 201 and body for a replay; rebuilt from the stored id, not a cached response.
    return {"order_id": order_id}


@app.get("/orders/{order_id}")
def read_order(
    request: Request, order_id: UUID, conn: Annotated[psycopg.Connection, Depends(db)]
):
    request.state.order_id = str(order_id)
    order = get_order(conn, str(order_id))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order
