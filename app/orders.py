"""Stub. You implement this in Step 3; tests/test_outbox_atomicity.py is the contract."""


def create_order(
    conn, customer_id: str, amount_cents: int, idempotency_key: str | None = None
) -> str:
    raise NotImplementedError("create_order is not implemented yet")


# Separate so the sabotage test can patch a failure between the two inserts.
def insert_outbox(cur, aggregate_id: str, event_type: str, payload: dict) -> None:
    raise NotImplementedError("insert_outbox is not implemented yet")
