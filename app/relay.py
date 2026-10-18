"""Outbox relay: the only process that publishes to RabbitMQ.

Delivery is at-least-once. A crash after the broker confirms a message but before published_at
commits leaves the row unmarked, so the next relay publishes it again with the same message_id;
consumers dedupe on that id.
"""

import json
import logging
import os

import pika
import psycopg

from app import broker, faults, health
from app.db import get_conn
from app.log import configure_logging

log = logging.getLogger("app.relay")

BATCH_SIZE = int(os.environ.get("RELAY_BATCH_SIZE", "100"))
# Upper bound on how long a row can wait if a NOTIFY is missed (e.g. relay was restarting).
POLL_SECONDS = 1.0


def publish(channel, message_id: str, event_type: str, payload: dict) -> None:
    """Publish one event. With confirm_delivery on the channel, this returns only after the
    broker has the message, and raises if it was nacked or (mandatory) matched no queue."""
    channel.basic_publish(
        exchange=broker.exchange_name(),
        routing_key=event_type,
        body=json.dumps(payload),
        properties=pika.BasicProperties(
            message_id=message_id,
            correlation_id=payload.get("correlation_id"),
            type=event_type,
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
        ),
        mandatory=True,
    )


def publish_batch(conn: psycopg.Connection, channel) -> int:
    """Publish one batch of unpublished rows and mark them in one transaction. Returns the count."""
    try:
        with conn.cursor() as cur:
            # SKIP LOCKED lets several relays split the backlog: a row locked by one relay is
            # invisible to the others until that relay commits or dies.
            cur.execute(
                """
                SELECT id, message_id::text, aggregate_id::text, event_type, payload
                FROM outbox
                WHERE published_at IS NULL
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (BATCH_SIZE,),
            )
            rows = cur.fetchall()
            for _, message_id, order_id, event_type, payload in rows:
                publish(channel, message_id, event_type, payload)
                log.info(
                    "published %s",
                    event_type,
                    extra={"order_id": order_id, "correlation_id": payload.get("correlation_id")},
                )
                faults.crash_point("relay", "after_publish")
            if rows:
                cur.execute(
                    "UPDATE outbox SET published_at = now() WHERE id = ANY(%s)",
                    ([row[0] for row in rows],),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def main() -> None:
    configure_logging()
    conn = get_conn()
    listener = get_conn()
    listener.autocommit = True
    listener.execute("LISTEN outbox")
    connection = broker.connect()
    channel = connection.channel()
    channel.confirm_delivery()
    broker.declare_topology(channel)
    log.info("relay started")
    while True:
        while publish_batch(conn, channel) == BATCH_SIZE:
            pass
        health.beat()
        # Wakes on the insert trigger's NOTIFY, or after POLL_SECONDS as a fallback.
        for _ in listener.notifies(timeout=POLL_SECONDS, stop_after=1):
            pass
        # Lets pika answer broker heartbeats between batches.
        connection.process_data_events(time_limit=0)


if __name__ == "__main__":
    main()
