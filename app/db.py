import os

import psycopg


def get_conn() -> psycopg.Connection:
    """Plain connection. Transaction boundaries are YOUR job in create_order:
    psycopg3 opens a transaction implicitly; commit() / rollback() decide fate."""
    url = os.environ.get("DATABASE_URL", "postgresql://edops:edops@localhost:5432/edops")
    return psycopg.connect(url)
