"""SQLite store: schema, idempotent init, and per-request connections.

Stdlib `sqlite3` only -- no ORM (per tech-stack.md: a no-auth demo tool does not
justify SQLAlchemy/alembic or operating a relational DB). Two tables, with
integrity enforced at the DB level: NOT NULL columns, a `documents.project_id`
foreign key indexed and declared `ON DELETE CASCADE`, and `PRAGMA foreign_keys`
turned ON for every connection so a project delete removes its document rows.

Each project carries an `index_name` (its per-project OpenSearch index,
`proj-{id}`) so the frontend can scope chat to it. The column is added by an
idempotent startup migration (an `ALTER TABLE` guarded by `PRAGMA table_info`)
so existing databases gain it without a manual step; existing rows are
backfilled with the deterministic `proj-{id}` name.

Project-level OpenSearch index cleanup on delete IS in scope (the BFF calls
ingestion's delete endpoint over HTTP); per-document chunk-level cleanup is
still out of scope.
"""

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    index_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename       TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    storage_uri    TEXT NOT NULL,
    status         TEXT NOT NULL,
    ingest_doc_id  TEXT,
    sha256         TEXT,
    chunks_indexed INTEGER,
    skipped        INTEGER,
    failure_reason TEXT,
    failure_code   INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_project_id ON documents(project_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with row access by name and cascade deletes enabled."""
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_index_name(conn: sqlite3.Connection) -> None:
    """Add `projects.index_name` if absent and backfill existing rows (idempotent).

    A pre-migration database created `projects` without `index_name`; add it
    with a guarded `ALTER TABLE` and backfill the deterministic `proj-{id}`
    name so older projects gain a valid scope without a manual step.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "index_name" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN index_name TEXT")
    conn.execute(
        "UPDATE projects SET index_name = 'proj-' || id WHERE index_name IS NULL"
    )


def init_db(db_path: str) -> None:
    """Create the schema if absent. Safe to run on every startup (idempotent)."""
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        _migrate_index_name(conn)
        conn.commit()
    finally:
        conn.close()
