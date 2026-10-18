"""Apply db/init.sql. Idempotent (IF NOT EXISTS / OR REPLACE / ON CONFLICT), so it can run on
every deploy. It creates missing objects but never alters existing ones: there is no migration
tool, so a changed column on a live database needs a manual change."""

from pathlib import Path

import psycopg

from app.db import get_conn

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "init.sql"


def apply(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    conn.commit()


def main() -> None:
    conn = get_conn()
    try:
        apply(conn)
    finally:
        conn.close()
    print(f"applied {SCHEMA.name}")


if __name__ == "__main__":
    main()
