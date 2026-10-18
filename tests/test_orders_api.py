"""API idempotency and order reads (API-1, API-2, API-4). Traces to INV-1 and INV-2."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BODY = {"customer_id": "cust-1", "amount_cents": 1999, "items": [{"sku": "SKU-1", "quantity": 2}]}


def count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def post(body, key="key-1", headers=None):
    h = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/orders", json=body, headers=h | (headers or {}))


def test_post_creates_order_and_one_outbox_event(conn):
    r = post(BODY)
    assert r.status_code == 201
    assert set(r.json()) == {"order_id"}
    assert count(conn, "orders") == 1
    with conn.cursor() as cur:
        cur.execute("SELECT aggregate_id::text, event_type FROM outbox")
        assert cur.fetchall() == [(r.json()["order_id"], "OrderCreated")]


def test_replay_same_key_same_body_returns_original_response(conn):
    first = post(BODY)
    second = post(BODY)
    assert second.status_code == first.status_code == 201
    assert second.content == first.content
    assert count(conn, "orders") == 1
    assert count(conn, "outbox") == 1


def test_same_key_different_body_is_409_and_writes_nothing(conn):
    post(BODY)
    r = post(BODY | {"amount_cents": 5000})
    assert r.status_code == 409
    assert count(conn, "orders") == 1
    assert count(conn, "outbox") == 1


def test_same_key_different_customers_are_independent(conn):
    a = post(BODY)
    b = post(BODY | {"customer_id": "cust-2"})
    assert a.status_code == b.status_code == 201
    assert a.json() != b.json()
    assert count(conn, "orders") == 2


@pytest.mark.parametrize("key", [None, ""])
def test_missing_idempotency_key_is_400(conn, key):
    r = post(BODY, key=key)
    assert r.status_code == 400
    assert count(conn, "orders") == 0


def test_get_order_returns_status_and_saga_step(conn):
    order_id = post(BODY).json()["order_id"]
    r = client.get(f"/orders/{order_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["order_id"] == order_id
    assert body["status"] == "PENDING"
    assert body["saga_step"] == "PAYMENT_REQUESTED"
    assert body["items"] == BODY["items"]


def test_get_unknown_order_is_404(conn):
    assert client.get(f"/orders/{uuid.uuid4()}").status_code == 404


def test_correlation_id_is_echoed_and_propagated_into_event(conn):
    r = post(BODY, headers={"X-Correlation-ID": "corr-abc"})
    assert r.headers["X-Correlation-ID"] == "corr-abc"
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'correlation_id' FROM outbox")
        assert cur.fetchone()[0] == "corr-abc"


def test_correlation_id_is_generated_when_absent(conn):
    r = post(BODY)
    uuid.UUID(r.headers["X-Correlation-ID"])
