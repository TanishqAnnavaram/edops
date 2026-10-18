"""Payment worker (CON-1). Charges are simulated: the payments row IS the charge."""

from app import faults
from app.consumer import Context, run


def handle(ctx: Context) -> None:
    event = ctx.message.event_type
    if event == "OrderCreated":
        charge(ctx)
    elif event == "RefundPayment":
        refund(ctx)
    else:
        raise ValueError(f"payment worker cannot handle {event}")


def charge(ctx: Context) -> None:
    payload = ctx.message.payload
    if payload["customer_id"] == faults.PAYMENT_DECLINED:
        ctx.log.info("payment declined (injected)")
        ctx.emit("PaymentFailed", {"reason": "declined"})
        return
    # Deliberately a plain INSERT: redeliveries are stopped by the processed_messages dedupe,
    # and if that ever fails, UNIQUE (order_id, kind) raises instead of charging twice (INV-2).
    ctx.cur.execute(
        "INSERT INTO payments (order_id, kind, amount_cents) VALUES (%s, 'charge', %s)",
        (ctx.message.order_id, payload["amount_cents"]),
    )
    ctx.log.info("charged %d cents", payload["amount_cents"])
    ctx.emit("PaymentSucceeded", {"amount_cents": payload["amount_cents"]})


def refund(ctx: Context) -> None:
    order_id = ctx.message.order_id
    # Lock the charge row so two refund commands for one order serialize here.
    ctx.cur.execute(
        "SELECT amount_cents FROM payments WHERE order_id = %s AND kind = 'charge' FOR UPDATE",
        (order_id,),
    )
    charge_row = ctx.cur.fetchone()
    if charge_row is None:
        raise ValueError(f"refund requested for order {order_id}, which has no charge")
    ctx.cur.execute(
        """
        INSERT INTO payments (order_id, kind, amount_cents) VALUES (%s, 'refund', %s)
        ON CONFLICT (order_id, kind) DO NOTHING
        RETURNING id
        """,
        (order_id, charge_row[0]),
    )
    if ctx.cur.fetchone() is None:
        # Compensations are idempotent by state (SAGA-6): a second refund command is answered
        # again so the saga can still advance, but money moves once.
        ctx.log.info("already refunded")
    else:
        ctx.log.info("refunded %d cents", charge_row[0])
    ctx.emit("PaymentRefunded", {"amount_cents": charge_row[0]})


def main() -> None:
    run("payment", handle)


if __name__ == "__main__":
    main()
