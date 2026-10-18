import os

import pika
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# Tests use their own database so they can truncate freely while the compose stack (which uses
# DATABASE_URL) keeps running. Set before any app code reads DATABASE_URL; subprocesses inherit
# it. Same default as .env.example.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://edops:edops@localhost:5432/edops_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app import init_db  # noqa: E402
from app.db import get_conn  # noqa: E402
from tests.support import Bus, Procs  # noqa: E402

# Rule: whenever a table is added to db/init.sql, add it here.
TABLES = "processed_messages, payments, reservations, saga_state, inventory, outbox, orders"

# Same rows as the seed in db/init.sql; TRUNCATE wipes them, so every test re-seeds.
INVENTORY_SEED = [("SKU-1", 10), ("SKU-2", 5), ("SKU-EMPTY", 0)]


def _connect(url: str) -> psycopg.Connection:
    try:
        return psycopg.connect(url, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.exit(
            f"Postgres unreachable at {url}. Is `docker compose up -d` running?\n{e}",
            returncode=1,
        )


@pytest.fixture(scope="session")
def test_database():
    """Create the test database if needed and rebuild its schema from db/init.sql once per run,
    so schema changes reach the tests without `docker compose down -v`."""
    dbname = conninfo_to_dict(TEST_DATABASE_URL)["dbname"]
    admin = _connect(make_conninfo(TEST_DATABASE_URL, dbname="postgres"))
    admin.autocommit = True
    try:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()
    with _connect(TEST_DATABASE_URL) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        init_db.apply(c)


@pytest.fixture
def clean_tables(test_database):
    """Truncate on a dedicated connection so the test's own connection starts clean."""
    with _connect(TEST_DATABASE_URL) as c:
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        c.cursor().executemany(
            "INSERT INTO inventory (sku, stock) VALUES (%s, %s)", INVENTORY_SEED
        )


@pytest.fixture
def conn(clean_tables):
    c = get_conn()
    yield c
    c.close()


@pytest.fixture
def bus(monkeypatch):
    """A per-test exchange and queue namespace; processes started afterwards inherit it."""
    b = Bus()
    monkeypatch.setenv("EDOPS_BROKER_PREFIX", b.prefix)
    try:
        b.open()
    except pika.exceptions.AMQPConnectionError as e:
        url = os.environ.get("RABBITMQ_URL", "amqp://edops:edops@localhost:5672/")
        pytest.exit(f"RabbitMQ unreachable at {url}. Is `docker compose up -d` running?\n{e}", 1)
    yield b
    b.close()


@pytest.fixture
def procs(conn, bus, tmp_path):
    p = Procs(tmp_path)
    yield p
    p.close()
