"""
db/database.py
==============
TIER 1 - DATA ACCESS LAYER.

Sole responsibility: own the MySQL connection pool and hand out transactional
sessions. It contains *no* business rules - it does not know what a "trade" or
a "portfolio" is. Only `services/*` imports this module; `app.py` must never
import it directly.

Why a pool at all?
    Streamlit re-runs the whole script on every widget interaction. Opening a
    fresh TCP + auth handshake to MySQL on each rerun would add ~30-80 ms per
    click and exhaust `max_connections` under a handful of users. SQLAlchemy's
    QueuePool keeps warm connections and hands them back out.

Key exports
-----------
get_engine()            -> lazily built, process-wide SQLAlchemy Engine
session_scope()         -> context manager: commit on success, rollback on error
connection_scope()      -> context manager for read-only / autocommit work
fetch_df(sql, params)   -> pandas.DataFrame  (the workhorse for analytics)
fetch_one(sql, params)  -> dict | None
execute(sql, params)    -> rowcount (auto-committed, single statement)
healthcheck()           -> (bool, message)
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional
from urllib.parse import quote_plus

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()  # reads .env sitting next to app.py

logger = logging.getLogger(__name__)

# Guards lazy singleton construction. Streamlit can spawn several script
# threads; without the lock two of them could build two engines (= two pools).
_ENGINE_LOCK = threading.Lock()
_ENGINE: Optional[Engine] = None
_SESSION_FACTORY: Optional[sessionmaker] = None


class DatabaseConfigError(RuntimeError):
    """Raised when required DB environment variables are missing/invalid."""


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    """Read an environment variable, with an explicit error if it is mandatory."""
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise DatabaseConfigError(
            f"Environment variable {name!r} is not set. "
            "Copy .env.example to .env and fill in your MySQL credentials."
        )
    return value  # type: ignore[return-value]


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` if it is unparseable."""
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        logger.warning("Env var %s=%r is not an integer; using %d", name, raw, default)
        return default


def build_connection_url() -> str:
    """
    Assemble the SQLAlchemy URL from environment variables.

    The password is percent-encoded because passwords frequently contain
    characters (@, :, /, #) that would otherwise break URL parsing.
    """
    user = _env("DB_USER", required=True)
    password = quote_plus(_env("DB_PASSWORD", ""))
    host = _env("DB_HOST", "localhost")
    port = _env_int("DB_PORT", 3306)
    name = _env("DB_NAME", "portfolio_db")
    return f"mysql+mysqlconnector://{user}:{password}@{host}:{port}/{name}"


# ---------------------------------------------------------------------------
# Engine / session factory (lazy singletons)
# ---------------------------------------------------------------------------

def _ssl_connect_args() -> dict:
    """
    Build the TLS arguments handed to mysql-connector-python.

    Local MySQL needs nothing here. Every managed provider (Aiven, TiDB Cloud,
    Railway, AWS RDS) REQUIRES an encrypted connection and will refuse plain
    TCP, so deployment is driven by two environment variables:

      DB_SSL_CA          absolute path to the provider's ca.pem
      DB_SSL_CA_CONTENT  the certificate text itself - used on Streamlit
                         Community Cloud, where there is nowhere to upload a
                         file. The content is written to a temp file at
                         startup and that path is given to the driver.

    If neither is set but DB_SSL=1, the connection is still encrypted, just
    without verifying the server certificate against a pinned CA.
    """
    ca_path = os.getenv("DB_SSL_CA", "").strip()
    ca_content = os.getenv("DB_SSL_CA_CONTENT", "").strip()

    if not ca_path and ca_content:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False, encoding="utf-8"
        )
        # Secrets managers often flatten newlines into the literal characters
        # backslash-n; restore them or OpenSSL rejects the PEM.
        tmp.write(ca_content.replace("\\n", "\n"))
        tmp.close()
        ca_path = tmp.name
        logger.info("Wrote CA certificate from DB_SSL_CA_CONTENT to %s", ca_path)

    if ca_path:
        return {"ssl_ca": ca_path, "ssl_verify_cert": True, "ssl_disabled": False}
    if os.getenv("DB_SSL", "0") == "1":
        return {"ssl_disabled": False}
    return {}


def get_engine() -> Engine:
    """
    Return the process-wide Engine, creating it on first use.

    Pool settings worth knowing:
      pool_pre_ping=True  -> emits a cheap SELECT 1 before handing out a
                             connection, transparently replacing ones the
                             server has already dropped.
      pool_recycle        -> proactively closes connections older than N
                             seconds, which must be < MySQL wait_timeout.
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    with _ENGINE_LOCK:
        if _ENGINE is None:  # re-check inside the lock
            url = build_connection_url()
            _ENGINE = create_engine(
                url,
                pool_size=_env_int("DB_POOL_SIZE", 5),
                max_overflow=_env_int("DB_MAX_OVERFLOW", 10),
                pool_recycle=_env_int("DB_POOL_RECYCLE", 1800),
                pool_timeout=_env_int("DB_POOL_TIMEOUT", 30),
                pool_pre_ping=True,
                echo=_env("SQL_ECHO", "0") == "1",
                connect_args=_ssl_connect_args(),
                future=True,
            )
            logger.info("SQLAlchemy engine initialised for %s", url.split("@")[-1])
    return _ENGINE


def get_session_factory() -> sessionmaker:
    """Return the configured sessionmaker bound to the shared engine."""
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        with _ENGINE_LOCK:
            if _SESSION_FACTORY is None:
                _SESSION_FACTORY = sessionmaker(
                    bind=get_engine(),
                    autoflush=False,
                    expire_on_commit=False,
                    future=True,
                )
    return _SESSION_FACTORY


def dispose_engine() -> None:
    """Close every pooled connection. Useful in tests and on shutdown."""
    global _ENGINE, _SESSION_FACTORY
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            _ENGINE.dispose()
            logger.info("SQLAlchemy engine disposed")
        _ENGINE = None
        _SESSION_FACTORY = None


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------

@contextmanager
def session_scope() -> Iterator[Session]:
    """
    Transactional scope around a series of statements. THIS is the primitive
    that makes trades atomic.

        with session_scope() as session:
            session.execute(text("UPDATE Users ..."))
            session.execute(text("INSERT INTO Transactions ..."))
        # <- COMMIT happens here, once, only if no exception was raised

    Any exception triggers ROLLBACK and is re-raised, so a half-applied trade
    (cash debited, shares never credited) is impossible.
    """
    session: Session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Transaction rolled back")
        raise
    finally:
        session.close()


@contextmanager
def connection_scope() -> Iterator[Connection]:
    """Context manager for single read-only statements (no explicit txn needed)."""
    conn = get_engine().connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Convenience query helpers
# ---------------------------------------------------------------------------

def fetch_df(sql: str, params: Optional[Mapping[str, Any]] = None) -> pd.DataFrame:
    """
    Run a SELECT and return the result as a pandas DataFrame.

    This is the bridge between the relational tier and the analytics tier.
    On error it logs and returns an EMPTY DataFrame rather than raising, so a
    single failing panel cannot take the whole dashboard down. Callers that
    need hard failure should use `connection_scope()` directly.

    Always pass values via `params` (named :binds) - never f-string them into
    `sql`, or you have an SQL-injection hole.
    """
    try:
        with connection_scope() as conn:
            return pd.read_sql(text(sql), conn, params=dict(params or {}))
    except SQLAlchemyError as exc:
        logger.error("fetch_df failed: %s", exc)
        return pd.DataFrame()


def fetch_one(sql: str, params: Optional[Mapping[str, Any]] = None) -> Optional[dict]:
    """Run a SELECT and return the first row as a dict, or None."""
    try:
        with connection_scope() as conn:
            row = conn.execute(text(sql), dict(params or {})).mappings().first()
            return dict(row) if row else None
    except SQLAlchemyError as exc:
        logger.error("fetch_one failed: %s", exc)
        return None


def execute(sql: str, params: Optional[Mapping[str, Any]] = None) -> int:
    """
    Execute a single write statement in its own committed transaction.
    Multi-statement work belongs in `session_scope()` instead.
    """
    with session_scope() as session:
        result = session.execute(text(sql), dict(params or {}))
        return result.rowcount or 0


def healthcheck() -> tuple[bool, str]:
    """
    Verify the database is reachable and the expected tables exist.
    Returns (ok, human-readable message) - the UI renders this in the sidebar.
    """
    required = {"users", "stocks", "transactions", "holdings"}
    try:
        with connection_scope() as conn:
            conn.execute(text("SELECT 1"))
            rows = conn.execute(text("SHOW TABLES")).fetchall()
            present = {str(r[0]).lower() for r in rows}
            missing = required - present
            if missing:
                return False, f"Connected, but missing tables: {', '.join(sorted(missing))}. Run schema.sql."
            return True, "Database connected"
    except DatabaseConfigError as exc:
        return False, str(exc)
    except SQLAlchemyError as exc:
        return False, f"Database unreachable: {exc.__class__.__name__}"
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected in the UI
        return False, f"Unexpected DB error: {exc}"
