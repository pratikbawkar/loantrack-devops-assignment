"""Backend configuration, read from environment variables only.

Database location (in priority order):
1. DATABASE_URL, if set.
2. Otherwise built from DB_HOST, DB_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD.
   This lets Kubernetes put host/port in a ConfigMap and user/password in a Secret.

The connection string contains the password, so it is excluded from repr()
and must never be logged.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Mapping

from psycopg.conninfo import conninfo_to_dict, make_conninfo


class ConfigError(Exception):
    """Raised when configuration is missing or invalid. Messages name variables, never values."""


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)  # contains the password: never print or log
    pool_min_size: int = 1
    pool_max_size: int = 10
    connect_timeout_seconds: int = 3
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 30.0
    app_version: str = "unknown"
    served_by: str = "unknown"


_DB_PART_VARS = ("DB_HOST", "DB_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")


def _get_int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}")
    return value


def _get_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    # float() accepts "nan", "inf", "-inf" and overflows like "1e309"; nan also
    # slips past the <= 0 check below because every comparison with it is False.
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number")
    if value <= 0:
        raise ConfigError(f"{name} must be > 0")
    return value


def _database_url(env: Mapping[str, str]) -> str:
    url = env.get("DATABASE_URL", "").strip()
    if url:
        # Fail fast on a malformed URL. psycopg's parse error quotes the bad
        # fragment (which can be the password), so it must not travel with our
        # error: raising outside the except block leaves no __context__ attached.
        try:
            parsed = conninfo_to_dict(url)
            # libpq only rejects a bad port when connecting; check it here. Multi-host
            # URLs list ports comma-separated.
            ports = str(parsed.get("port") or "").split(",") if parsed.get("port") else []
            valid = all(p.strip().isdigit() and 0 < int(p) < 65536 for p in ports)
        except Exception:
            valid = False
        if not valid:
            raise ConfigError("DATABASE_URL is not a valid PostgreSQL connection string")
        return url

    missing = [name for name in _DB_PART_VARS if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "database configuration missing: set DATABASE_URL, or all of "
            + ", ".join(_DB_PART_VARS)
            + " (missing: "
            + ", ".join(missing)
            + ")"
        )
    _get_int(env, "DB_PORT", default=5432, minimum=1, maximum=65535)  # validate it is a valid port
    # make_conninfo quotes special characters in the password safely.
    return make_conninfo(
        host=env["DB_HOST"].strip(),
        port=env["DB_PORT"].strip(),
        dbname=env["POSTGRES_DB"].strip(),
        user=env["POSTGRES_USER"].strip(),
        password=env["POSTGRES_PASSWORD"],
    )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    min_size = _get_int(env, "DB_POOL_MIN_SIZE", default=1, minimum=0)
    max_size = _get_int(env, "DB_POOL_MAX_SIZE", default=10, minimum=1)
    if min_size > max_size:
        raise ConfigError("DB_POOL_MIN_SIZE must be <= DB_POOL_MAX_SIZE")

    retry_base = _get_float(env, "DB_RETRY_BASE_SECONDS", default=0.5)
    retry_max = _get_float(env, "DB_RETRY_MAX_SECONDS", default=30.0)
    if retry_base > retry_max:
        raise ConfigError("DB_RETRY_BASE_SECONDS must be <= DB_RETRY_MAX_SECONDS")

    return Settings(
        database_url=_database_url(env),
        pool_min_size=min_size,
        pool_max_size=max_size,
        connect_timeout_seconds=_get_int(env, "DB_CONNECT_TIMEOUT_SECONDS", default=3, minimum=1),
        retry_base_seconds=retry_base,
        retry_max_seconds=retry_max,
        app_version=env.get("APP_VERSION", "").strip() or "unknown",
        served_by=env.get("HOSTNAME", "").strip() or "unknown",
    )
