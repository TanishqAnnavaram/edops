import os

import psycopg
import pytest

from app.db import get_conn

# Same default as .env.example.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://edops:edops@localhost:5432/edops")

# Rule: whenever a table is added to db/init.sql, add it here.
TABLES = "outbox, orders"


@pytest.fixture
def clean_tables():
    """Truncate on a dedicated connection so the test's own connection starts clean."""
    try:
        c = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.exit(
            f"Postgres unreachable at {DATABASE_URL}. Is `docker compose up -d` running?\n{e}",
            returncode=1,
        )
    with c:
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")


@pytest.fixture
def conn(clean_tables):
    c = get_conn()
    yield c
    c.close()
