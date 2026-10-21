"""Demo dashboard endpoints (app/demo.py): off by default, safe by construction, and the
redeliver button does what the demo claims against the real relay and worker."""

import os
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from app.orders import create_order, insert_outbox
from tests.support import ROOT, scalar, wait_until

ITEMS = [{"sku": "SKU-1", "quantity": 1}]
client = TestClient(app)


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setenv("DEMO_UI", "1")


def test_demo_endpoints_are_404_unless_enabled(conn, monkeypatch):
    monkeypatch.delenv("DEMO_UI", raising=False)
    order_id = create_order(conn, "c1", 1999, "k1", items=ITEMS)

    assert client.get("/demo").status_code == 404
    assert client.get("/demo/api/state").status_code == 404
    assert client.get(f"/demo/api/orders/{order_id}/timeline").status_code == 404
    assert client.post(f"/demo/api/orders/{order_id}/redeliver").status_code == 404
    assert scalar("SELECT count(*) FROM outbox WHERE published_at IS NULL") == 1


def test_page_and_script_are_served_when_enabled(conn, demo_on):
    page = client.get("/demo")
    assert page.status_code == 200
    assert "<title>edops demo</title>" in page.text
    script = client.get("/demo/api/script").json()
    assert [s["id"] for s in script["steps"]] == [
        "place", "again", "conflict", "redeliver", "compensate",
    ]


def test_state_lists_recent_orders_and_stock(conn, demo_on):
    order_id = create_order(conn, "c1", 1999, "k1", items=ITEMS)

    state = client.get("/demo/api/state").json()

    assert state["orders"][0]["order_id"] == order_id
    assert state["orders"][0]["idempotency_key"] == "k1"
    assert (state["orders"][0]["charges"], state["orders"][0]["refunds"]) == (0, 0)
    stock = {s["sku"]: s["stock"] for s in state["stock"]}
    assert stock == {"SKU-1": 10, "SKU-2": 5, "SKU-EMPTY": 0}


def test_timeline_lists_the_orders_events(conn, demo_on):
    order_id = create_order(conn, "c1", 1999, "k1", items=ITEMS)

    events = client.get(f"/demo/api/orders/{order_id}/timeline").json()

    assert [e["event_type"] for e in events] == ["OrderCreated"]
    assert events[0]["published_at"] is None
    assert events[0]["processed_by"] is None


def test_redeliver_unmarks_only_order_created_and_wakes_the_relay(conn, demo_on):
    order_id = create_order(conn, "c1", 1999, "k1", items=ITEMS)
    with conn.cursor() as cur:
        insert_outbox(cur, order_id, "PaymentSucceeded", {"order_id": order_id})
        cur.execute("UPDATE outbox SET published_at = now()")
    conn.commit()
    listener = get_conn()
    listener.autocommit = True
    listener.execute("LISTEN outbox")

    r = client.post(f"/demo/api/orders/{order_id}/redeliver")

    assert r.status_code == 200
    notified = list(listener.notifies(timeout=5, stop_after=1))
    listener.close()
    assert len(notified) == 1, "redeliver must NOTIFY: the insert trigger does not fire on UPDATE"
    rows = dict(conn.execute("SELECT event_type, published_at FROM outbox").fetchall())
    conn.rollback()
    assert rows["OrderCreated"] is None
    assert rows["PaymentSucceeded"] is not None
    assert r.json()["message_id"] == scalar(
        "SELECT message_id::text FROM outbox WHERE event_type = 'OrderCreated'"
    )


def test_redeliver_unknown_order_is_404(conn, demo_on):
    r = client.post("/demo/api/orders/00000000-0000-0000-0000-000000000000/redeliver")
    assert r.status_code == 404


def test_redeliver_button_republishes_and_the_worker_charges_once(procs, demo_on):
    """INV-2, through the dashboard: the redeliver endpoint makes the real relay re-publish the
    same message, and the real payment worker skips it."""
    procs.start("app.relay", ready="relay started")
    payment = procs.start("app.workers.payment", ready="payment started")
    order_id = client.post(
        "/orders",
        json={"customer_id": "c1", "amount_cents": 1999, "items": ITEMS},
        headers={"Idempotency-Key": "k1"},
    ).json()["order_id"]
    charges = "SELECT count(*) FROM payments WHERE order_id = %s AND kind = 'charge'"
    wait_until(lambda: scalar(charges, (order_id,)) == 1, 15, "never charged", procs.dump)

    r = client.post(f"/demo/api/orders/{order_id}/redeliver").json()

    wait_until(
        lambda: scalar(
            "SELECT published_at > %s FROM outbox WHERE message_id = %s",
            (r["requested_at"], r["message_id"]),
        ),
        5,
        "relay did not re-publish promptly after redeliver",
        procs.dump,
    )
    payment.wait_for_log(f"duplicate OrderCreated skipped message_id={r['message_id']}")
    assert scalar(charges, (order_id,)) == 1


@pytest.mark.parametrize("marker", ["RAILWAY_ENVIRONMENT", "RENDER"])
def test_app_refuses_to_start_with_demo_on_a_hosting_platform(marker):
    env = os.environ | {"DEMO_UI": "1", marker: "production"}
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "must not run on a hosting platform" in result.stderr


def test_app_starts_with_demo_on_locally():
    env = {k: v for k, v in os.environ.items() if k not in ("RAILWAY_ENVIRONMENT", "RENDER")}
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"], cwd=ROOT, env=env | {"DEMO_UI": "1"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_deploy_config_never_sets_demo_ui():
    for path in (".railway/railway.ts", ".github/workflows/ci.yml"):
        assert "DEMO_UI" not in (ROOT / path).read_text(encoding="utf-8"), path
    guide = (ROOT / "docs/deploy.md").read_text(encoding="utf-8")
    # Not as an assignment, and not as a row of a variables table.
    assert not re.search(r"DEMO_UI\s*[=:]", guide)
    assert not re.search(r"^\|\s*`?DEMO_UI", guide, re.MULTILINE)
    # Prose may only mention it to forbid it (checked per paragraph: Markdown wraps lines).
    for paragraph in guide.split("\n\n"):
        if "DEMO_UI" in paragraph:
            assert "never set" in paragraph.lower(), f"deploy.md mentions DEMO_UI:\n{paragraph}"
