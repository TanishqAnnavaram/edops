"""Idempotent consumer loop shared by every worker and the orchestrator.

Per message, in ONE transaction: insert (consumer, message_id) into processed_messages, run the
handler (business rows plus outbox rows), commit. Ack only after the commit. A message whose id
is already recorded is a redelivery: it is acked without running the handler.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from app import broker, faults, health
from app.db import get_conn
from app.log import configure_logging, correlation_id_var
from app.orders import insert_outbox


@dataclass
class Message:
    message_id: str
    event_type: str
    payload: dict
    correlation_id: str | None

    @property
    def order_id(self) -> str:
        return self.payload["order_id"]


class Context:
    """What a handler may do: write rows through cur and emit events through the outbox."""

    def __init__(self, cur: psycopg.Cursor, message: Message, log: logging.LoggerAdapter):
        self.cur = cur
        self.message = message
        self.log = log

    def emit(self, event_type: str, payload: dict) -> None:
        payload = {"order_id": self.message.order_id} | payload
        payload["correlation_id"] = self.message.correlation_id
        insert_outbox(self.cur, payload["order_id"], event_type, payload)
        self.log.info("emitted %s", event_type)


Handler = Callable[[Context], None]


def process(conn: psycopg.Connection, name: str, message: Message, handler: Handler, log) -> bool:
    """Run one message in one transaction. Returns False if it was a duplicate."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO processed_messages (consumer, message_id) VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                (name, message.message_id),
            )
            if cur.fetchone() is None:
                conn.rollback()
                log.info(
                    "duplicate %s skipped message_id=%s", message.event_type, message.message_id
                )
                return False
            handler(Context(cur, message, log))
            faults.crash_point(name, "before_commit")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def run(name: str, handler: Handler) -> None:
    configure_logging()
    logger = logging.getLogger(f"app.{name}")
    conn = get_conn()
    connection = broker.connect()
    channel = connection.channel()
    broker.declare_topology(channel)
    # One unacked message at a time: a crash can then leave at most one message in doubt.
    channel.basic_qos(prefetch_count=1)

    def on_message(ch, method, properties, body):
        message = Message(
            message_id=properties.message_id,
            event_type=properties.type,
            payload=json.loads(body),
            correlation_id=properties.correlation_id,
        )
        token = correlation_id_var.set(message.correlation_id)
        log = logging.LoggerAdapter(logger, {"order_id": message.payload.get("order_id")})
        try:
            log.info("received %s message_id=%s", message.event_type, message.message_id)
            try:
                processed = process(conn, name, message, handler, log)
            except Exception:
                # Requeue with no limit or backoff: a message that always fails loops forever.
                # Dropping it instead would lose the order (INV-1). Retries/DLQ are deferred.
                log.exception("failed %s, requeueing", message.event_type)
                ch.basic_nack(method.delivery_tag, requeue=True)
                return
            if processed:
                faults.crash_point(name, "after_commit")
                faults.hang_point(name, "after_commit")
            ch.basic_ack(method.delivery_tag)
        finally:
            correlation_id_var.reset(token)

    channel.basic_consume(broker.queue_name(name), on_message)
    logger.info("%s started", name)
    while True:
        connection.process_data_events(time_limit=1)
        health.beat()
