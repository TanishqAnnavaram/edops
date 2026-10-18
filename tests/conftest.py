import pytest

from app.db import get_conn


@pytest.fixture
def conn():
    """Fresh connection; tables are truncated before each test."""
    c = get_conn()
    with c.cursor() as cur:
        cur.execute("TRUNCATE outbox, orders RESTART IDENTITY CASCADE")
    c.commit()
    yield c
    c.close()
