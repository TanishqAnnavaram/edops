"""Inventory worker (CON-2): reserves stock all-or-nothing, releases it idempotently."""

from collections import Counter

from app import faults
from app.consumer import Context, run


def handle(ctx: Context) -> None:
    event = ctx.message.event_type
    if event == "ReserveStock":
        reserve(ctx)
    elif event == "ReleaseStock":
        release(ctx)
    else:
        raise ValueError(f"inventory worker cannot handle {event}")


def reserve(ctx: Context) -> None:
    payload = ctx.message.payload
    if payload["customer_id"] == faults.STOCK_REJECTED:
        ctx.log.info("stock rejected (injected)")
        ctx.emit("StockRejected", {"reason": "injected"})
        return
    wanted = Counter()
    for item in payload["items"]:
        wanted[item["sku"]] += item["quantity"]

    # Savepoint so a shortfall on the 2nd SKU undoes the 1st SKU's decrement, while the
    # processed_messages row and the StockRejected event still commit.
    ctx.cur.execute("SAVEPOINT reserve")
    # Sorted so two orders sharing SKUs lock rows in the same order and cannot deadlock.
    for sku in sorted(wanted):
        ctx.cur.execute(
            "UPDATE inventory SET stock = stock - %s WHERE sku = %s AND stock >= %s",
            (wanted[sku], sku, wanted[sku]),
        )
        if ctx.cur.rowcount == 0:
            ctx.cur.execute("ROLLBACK TO SAVEPOINT reserve")
            ctx.log.info("stock rejected: %s unknown or short of %d", sku, wanted[sku])
            ctx.emit("StockRejected", {"reason": f"insufficient stock for {sku}"})
            return
        ctx.cur.execute(
            """
            INSERT INTO reservations (order_id, sku, quantity, status)
            VALUES (%s, %s, %s, 'reserved')
            """,
            (ctx.message.order_id, sku, wanted[sku]),
        )
    ctx.cur.execute("RELEASE SAVEPOINT reserve")
    ctx.log.info("reserved %s", dict(wanted))
    ctx.emit("StockReserved", {"items": [{"sku": s, "quantity": q} for s, q in wanted.items()]})


def release(ctx: Context) -> None:
    # The status flip is the guard (INV-3): only rows still 'reserved' are returned, so a
    # second release gives stock back zero times.
    ctx.cur.execute(
        """
        UPDATE reservations SET status = 'released', released_at = now()
        WHERE order_id = %s AND status = 'reserved'
        RETURNING sku, quantity
        """,
        (ctx.message.order_id,),
    )
    released = ctx.cur.fetchall()
    for sku, quantity in released:
        ctx.cur.execute(
            "UPDATE inventory SET stock = stock + %s WHERE sku = %s", (quantity, sku)
        )
    ctx.log.info("released %d reservation(s)", len(released))
    ctx.emit("StockReleased", {"released": len(released)})


def main() -> None:
    run("inventory", handle)


if __name__ == "__main__":
    main()
