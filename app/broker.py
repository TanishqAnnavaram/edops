"""RabbitMQ connection and topology. Every process declares the topology on startup."""

import os

import pika

EXCHANGE = "events"

# Queue per consumer and the event types it receives. Declared by the relay too, so a message
# published before its consumer first starts waits in a durable queue instead of being dropped.
QUEUES: dict[str, list[str]] = {
    "payment": ["OrderCreated", "RefundPayment"],
    "inventory": ["ReserveStock", "ReleaseStock"],
    "orchestrator": [
        "PaymentSucceeded",
        "PaymentFailed",
        "StockReserved",
        "StockRejected",
        "StockReleased",
        "PaymentRefunded",
    ],
}


def prefix() -> str:
    # Tests set a unique prefix so their exchange and queues never meet the compose workers'.
    return os.environ.get("EDOPS_BROKER_PREFIX", "")


def exchange_name() -> str:
    return prefix() + EXCHANGE


def queue_name(consumer: str) -> str:
    return prefix() + consumer


def connect() -> pika.BlockingConnection:
    url = os.environ.get("RABBITMQ_URL", "amqp://edops:edops@localhost:5672/")
    return pika.BlockingConnection(pika.URLParameters(url))


def declare_topology(channel) -> None:
    channel.exchange_declare(exchange_name(), exchange_type="topic", durable=True)
    for consumer, event_types in QUEUES.items():
        channel.queue_declare(queue_name(consumer), durable=True)
        for event_type in event_types:
            channel.queue_bind(queue_name(consumer), exchange_name(), routing_key=event_type)
