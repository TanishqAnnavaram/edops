"""Log-only consumer: logs every event on the exchange. A dev and test tool, not deployed."""

import json
import logging

from app import broker
from app.log import configure_logging

log = logging.getLogger("app.audit")


def main() -> None:
    configure_logging()
    connection = broker.connect()
    channel = connection.channel()
    broker.declare_topology(channel)
    # Exclusive and auto-deleted: the queue exists only while audit runs, so an idle audit
    # queue can never fill up in a long-running environment.
    queue = channel.queue_declare("", exclusive=True).method.queue
    channel.queue_bind(queue, broker.exchange_name(), routing_key="#")

    def on_message(ch, method, properties, body):
        payload = json.loads(body)
        log.info(
            "saw %s message_id=%s",
            properties.type,
            properties.message_id,
            extra={
                "order_id": payload.get("order_id"),
                "correlation_id": properties.correlation_id,
            },
        )
        ch.basic_ack(method.delivery_tag)

    channel.basic_consume(queue, on_message)
    log.info("audit started")
    channel.start_consuming()


if __name__ == "__main__":
    main()
