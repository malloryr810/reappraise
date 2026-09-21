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
_NAME_HEADER = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)
_COMMENT_LINE = re.compile(r"^\s*--.*$", re.MULTILINE)
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


def schema_statements() -> list[str]:
    text = _strip_comments((_SQL_DIR / "schema.sql").read_text())
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


def init_schema(config: DbConfig) -> None:
    """Create any missing tables. Safe to run repeatedly."""
    with connect(config) as conn, conn.cursor() as cur:
        for stmt in schema_statements():
            cur.execute(stmt)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    if sys.argv[1:] != ["init"]:
        sys.exit("usage: python db.py init")
    cfg = DbConfig.from_env()
    if cfg is None:
        sys.exit("MYSQL_DATABASE is not set — see .env.example")
    init_schema(cfg)
    print(f"Schema ready in {cfg.database}@{cfg.host}:{cfg.port}")
