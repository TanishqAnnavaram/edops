"""Smoke test a deployed edops (DEP-4): post one order twice with the same Idempotency-Key and
show there is one order and exactly one charge.

    python scripts/smoke.py https://<your-app>.up.railway.app

Standard library only, so it runs from any machine with Python 3.12.
"""

import argparse
import json
import sys
import time
import urllib.request
import uuid

TERMINAL_STEPS = {"CONFIRMED", "FAILED", "COMPENSATED"}


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("--timeout", type=float, default=60, help="seconds to wait for the saga")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    key = f"smoke-{uuid.uuid4()}"
    body = {
        "customer_id": f"smoke-{int(time.time())}",
        "amount_cents": 1999,
        "items": [{"sku": "SKU-1", "quantity": 1}],
    }
    headers = {"Idempotency-Key": key, "X-Correlation-ID": key}

    first_status, first = call("POST", f"{base}/orders", body, headers)
    second_status, second = call("POST", f"{base}/orders", body, headers)
    print(f"Idempotency-Key: {key}")
    print(f"POST #1 -> {first_status} order_id={first['order_id']}")
    print(f"POST #2 -> {second_status} order_id={second['order_id']}")

    order_id = first["order_id"]
    deadline = time.monotonic() + args.timeout
    while True:
        _, order = call("GET", f"{base}/orders/{order_id}")
        if order["saga_step"] in TERMINAL_STEPS or time.monotonic() >= deadline:
            break
        time.sleep(1)

    print(f"status={order['status']} saga_step={order['saga_step']}")
    print(f"charges={order['charges']} refunds={order['refunds']}")

    ok = (
        first["order_id"] == second["order_id"]
        and order["saga_step"] == "CONFIRMED"
        and order["charges"] == 1
    )
    print("RESULT: PASS, one order and exactly one charge" if ok else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
