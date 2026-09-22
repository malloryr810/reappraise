# MySQL access layer: connection config from env, a transactional connection
# context manager, and a loader that reads named statements from sql/queries.sql
# so all SQL lives in .sql files rather than scattered through Python strings.
#
# Persistence is optional — if MYSQL_DATABASE is unset, DbConfig.from_env()
# returns None and the app runs exactly as before, without saving anything.

import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

_SQL_DIR = Path(__file__).parent / "sql"
_MIGRATIONS_DIR = _SQL_DIR / "migrations"
_NAME_HEADER = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)
_COMMENT_LINE = re.compile(r"^\s*--.*$", re.MULTILINE)
_MIGRATION_NAME = re.compile(r"^\d{3}_\w+\.sql$")
_CONNECT_TIMEOUT_S = 5


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls, database_var: str = "MYSQL_DATABASE") -> "DbConfig | None":
        database = os.getenv(database_var)
        if not database:
            return None
        return cls(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=database,
        )


@contextmanager
def connect(config: DbConfig) -> Iterator[Connection]:
    """Yield a connection that commits on clean exit and rolls back on error.

    Everything done inside one `with connect(...)` block is a single
    transaction, so a failure halfway through an appraisal never leaves an
    item without its listings or estimate.
    """
    conn = pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=_CONNECT_TIMEOUT_S,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _strip_comments(sql: str) -> str:
    return _COMMENT_LINE.sub("", sql).strip()


@cache
def _load_queries() -> dict[str, str]:
    text = (_SQL_DIR / "queries.sql").read_text()
    # split() with a capture group yields [preamble, name1, body1, name2, body2, ...]
    parts = _NAME_HEADER.split(text)
    return {
        name: _strip_comments(body).rstrip(";").strip()
        for name, body in zip(parts[1::2], parts[2::2])
    }


def query(name: str) -> str:
    """Return the SQL for a `-- name:` block in sql/queries.sql."""
    try:
        return _load_queries()[name]
    except KeyError:
        raise KeyError(f"No query named {name!r} in sql/queries.sql") from None


def _statements(path: Path) -> list[str]:
    text = _strip_comments(path.read_text())
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


def schema_statements() -> list[str]:
    return _statements(_SQL_DIR / "schema.sql")


def migration_files() -> list[tuple[str, Path]]:
    """(version, path) for every sql/migrations/NNN_*.sql file, oldest first.

    A .sql file that doesn't follow the NNN_name.sql pattern raises instead of
    being skipped — a silently ignored migration would leave the schema behind.
    """
    paths = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    misnamed = [path.name for path in paths if not _MIGRATION_NAME.match(path.name)]
    if misnamed:
        raise ValueError(f"Migration files must be named NNN_description.sql: {misnamed}")
    return [(path.stem, path) for path in paths]


def applied_migrations(conn: Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(query("applied_migrations"))
        return {row["version"] for row in cur.fetchall()}


def init_schema(config: DbConfig) -> list[str]:
    """Create any missing tables, then apply pending migrations in order.

    Safe to run repeatedly. Returns the versions applied by this call. MySQL
    commits each ALTER TABLE implicitly, so every migration is recorded as soon
    as it runs; a failure stops the run and leaves later migrations pending.
    """
    applied_now: list[str] = []
    with connect(config) as conn:
        with conn.cursor() as cur:
            for stmt in schema_statements():
                cur.execute(stmt)
        done = applied_migrations(conn)
        for version, path in migration_files():
            if version in done:
                continue
            with conn.cursor() as cur:
                for stmt in _statements(path):
                    cur.execute(stmt)
                cur.execute(query("record_migration"), {"version": version})
            conn.commit()
            applied_now.append(version)
    return applied_now


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    if sys.argv[1:] != ["init"]:
        sys.exit("usage: python db.py init")
    cfg = DbConfig.from_env()
    if cfg is None:
        sys.exit("MYSQL_DATABASE is not set — see .env.example")
    applied = init_schema(cfg)
    print(f"Applied migrations: {', '.join(applied)}" if applied else "No pending migrations")
    print(f"Schema ready in {cfg.database}@{cfg.host}:{cfg.port}")
