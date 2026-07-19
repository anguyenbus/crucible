"""Request-scoped dependencies."""

from collections.abc import Iterator
from sqlite3 import Connection

from app.config import get_settings
from app.db import connect


def get_conn() -> Iterator[Connection]:
    """Yield a per-request SQLite connection (foreign keys ON), closed after."""
    conn = connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()
