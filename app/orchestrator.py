"""Saga orchestrator (SAGA-1): owns the order flow and saga_state.

Each event is one transaction (via app.consumer): lock the saga row, look up (step, event) in
TRANSITIONS, update saga_state (and orders.status on terminal steps), write the next command to
the outbox. A pair not in the table is a duplicate, late or out-of-order event: logged, ignored.
The table must match docs/design.md section 2.
"""

from dataclasses import dataclass

from app import faults
from app.consumer import Context, run


@dataclass(frozen=True)
class Transition:
    next_step: str
    # Set only on terminal steps; it changes in the same transaction as the step.
    order_status: str | None = None
    command: str | None = None


TRANSITIONS: dict[tuple[str, str], Transition] = {
    ("PAYMENT_REQUESTED", "PaymentSucceeded"): Transition("STOCK_REQUESTED", None, "ReserveStock"),
    ("PAYMENT_REQUESTED", "PaymentFailed"): Transition("FAILED", "FAILED"),
    ("STOCK_REQUESTED", "StockReserved"): Transition("CONFIRMED", "CONFIRMED"),
    ("STOCK_REQUESTED", "StockRejected"): Transition("REFUND_REQUESTED", None, "RefundPayment"),
    ("RELEASE_REQUESTED", "StockReleased"): Transition("REFUND_REQUESTED", None, "RefundPayment"),
    ("REFUND_REQUESTED", "PaymentRefunded"): Transition("COMPENSATED", "FAILED"),
}

# T4f: the post-reservation check rejected the order, so the held stock goes back first.
AFTER_RESERVE_FAILED = Transition("RELEASE_REQUESTED", None, "ReleaseStock")


def handle(ctx: Context) -> None:
    order_id = ctx.message.order_id
    event = ctx.message.event_type
    # FOR UPDATE serializes events for one order, so two replies cannot both advance one step.
    ctx.cur.execute(
        """
        SELECT s.step, o.customer_id, o.items
        FROM saga_state s JOIN orders o ON o.id = s.order_id
        WHERE s.order_id = %s
        FOR UPDATE OF s
        """,
        (order_id,),
    )
    row = ctx.cur.fetchone()
    if row is None:
        ctx.log.warning("ignored %s: no saga for this order", event)
        return
    step, customer_id, items = row

    transition = TRANSITIONS.get((step, event))
    if transition is None:
        ctx.log.info("ignored %s in step %s", event, step)
        return
    if transition.next_step == "CONFIRMED" and customer_id == faults.FAIL_AFTER_RESERVE:
        ctx.log.info("post-reservation check failed (injected)")
        transition = AFTER_RESERVE_FAILED

    ctx.cur.execute(
        "UPDATE saga_state SET step = %s, updated_at = now() WHERE order_id = %s",
        (transition.next_step, order_id),
    )
    if transition.order_status:
        ctx.cur.execute(
            "UPDATE orders SET status = %s, updated_at = now() WHERE id = %s",
            (transition.order_status, order_id),
        )
    ctx.log.info("%s -> %s on %s", step, transition.next_step, event)
    if transition.command == "ReserveStock":
        ctx.emit("ReserveStock", {"customer_id": customer_id, "items": items})
    elif transition.command:
        ctx.emit(transition.command, {})


def main() -> None:
    run("orchestrator", handle)


if __name__ == "__main__":
    main()
