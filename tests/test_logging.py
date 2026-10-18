"""OBS-1: every log line is JSON with correlation_id and order_id."""
import json
import logging

from fastapi.testclient import TestClient

from app.log import JsonFormatter, correlation_id_var
from app.main import app
from app.orders import create_order


def test_formatter_emits_json_with_correlation_and_order_id():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    record.order_id = "order-1"
    token = correlation_id_var.set("corr-1")
    try:
        line = json.loads(JsonFormatter().format(record))
    finally:
        correlation_id_var.reset(token)
    assert line["msg"] == "hello x"
    assert line["correlation_id"] == "corr-1"
    assert line["order_id"] == "order-1"


def test_create_order_logs_with_order_id(conn, caplog):
    caplog.set_level(logging.INFO, logger="app.orders")
    order_id = create_order(conn, "cust-1", 1999, "key-1")
    assert any(getattr(r, "order_id", None) == order_id for r in caplog.records)


def test_request_log_line_carries_order_id(conn, caplog):
    caplog.set_level(logging.INFO, logger="app.api")
    r = TestClient(app).post(
        "/orders",
        json={"customer_id": "c1", "amount_cents": 500, "items": [{"sku": "SKU-1", "quantity": 1}]},
        headers={"Idempotency-Key": "k1"},
    )
    request_lines = [rec for rec in caplog.records if rec.getMessage().startswith("POST /orders")]
    assert len(request_lines) == 1
    assert request_lines[0].order_id == r.json()["order_id"]
