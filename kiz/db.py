from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
)


@contextmanager
def tx():
    """Connection inside one transaction: committed on success, rolled back on error."""
    with pool.connection() as conn:
        with conn.transaction():
            yield conn


def init():
    pool.open(wait=True)
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    with tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4242)")
        conn.execute(schema)


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(value)),
    )
