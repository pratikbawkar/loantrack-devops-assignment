"""Configuration: env vars only, DATABASE_URL precedence, no secrets in repr or errors."""

import logging
import traceback

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.config import ConfigError, load_settings
from app.main import configure_logging

from conftest import BASE_ENV, SECRET_PASSWORD

DB_PARTS = {k: BASE_ENV[k] for k in ("DB_HOST", "DB_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")}


def test_builds_url_from_parts():
    settings = load_settings(DB_PARTS)
    parts = conninfo_to_dict(settings.database_url)
    assert parts == {
        "host": "db.internal.example",
        "port": "5432",
        "dbname": "loantrack",
        "user": "loantrack_user",
        "password": SECRET_PASSWORD,
    }


def test_database_url_takes_precedence_over_parts():
    url = "postgresql://other_user:other_pw@localhost:5433/otherdb"
    settings = load_settings({**DB_PARTS, "DATABASE_URL": url})
    assert settings.database_url == url


def test_empty_database_url_falls_back_to_parts():
    settings = load_settings({**DB_PARTS, "DATABASE_URL": "   "})
    assert conninfo_to_dict(settings.database_url)["host"] == "db.internal.example"


def test_compose_style_env_points_at_service_name_and_internal_port():
    env = {**DB_PARTS, "DB_HOST": "postgres", "DB_PORT": "5432"}
    parts = conninfo_to_dict(load_settings(env).database_url)
    assert (parts["host"], parts["port"]) == ("postgres", "5432")


def test_password_with_special_characters_is_quoted_safely():
    tricky = "p@ss word'with\\specials=;"
    settings = load_settings({**DB_PARTS, "POSTGRES_PASSWORD": tricky})
    assert conninfo_to_dict(settings.database_url)["password"] == tricky


def test_missing_parts_error_names_variables_not_values():
    env = dict(DB_PARTS)
    del env["POSTGRES_USER"]
    del env["DB_HOST"]
    with pytest.raises(ConfigError) as info:
        load_settings(env)
    message = str(info.value)
    assert "POSTGRES_USER" in message and "DB_HOST" in message
    assert SECRET_PASSWORD not in message


def test_nothing_configured_raises():
    with pytest.raises(ConfigError):
        load_settings({})


def test_defaults():
    s = load_settings(DB_PARTS)
    assert (s.pool_min_size, s.pool_max_size) == (1, 10)
    assert s.connect_timeout_seconds == 3
    assert (s.retry_base_seconds, s.retry_max_seconds) == (0.5, 30.0)
    assert s.app_version == "unknown"
    assert s.served_by == "unknown"


def test_pool_retry_version_and_hostname_from_env():
    s = load_settings(
        {
            **DB_PARTS,
            "DB_POOL_MIN_SIZE": "2",
            "DB_POOL_MAX_SIZE": "7",
            "DB_CONNECT_TIMEOUT_SECONDS": "4",
            "DB_RETRY_BASE_SECONDS": "0.25",
            "DB_RETRY_MAX_SECONDS": "10",
            "APP_VERSION": "v2",
            "HOSTNAME": "backend-7d9f-xyz",
        }
    )
    assert (s.pool_min_size, s.pool_max_size, s.connect_timeout_seconds) == (2, 7, 4)
    assert (s.retry_base_seconds, s.retry_max_seconds) == (0.25, 10.0)
    assert (s.app_version, s.served_by) == ("v2", "backend-7d9f-xyz")


@pytest.mark.parametrize(
    "overrides",
    [
        {"DB_POOL_MAX_SIZE": "ten"},
        {"DB_POOL_MAX_SIZE": "0"},
        {"DB_POOL_MIN_SIZE": "5", "DB_POOL_MAX_SIZE": "2"},
        {"DB_RETRY_BASE_SECONDS": "-1"},
        {"DB_RETRY_BASE_SECONDS": "60", "DB_RETRY_MAX_SECONDS": "30"},
        {"DB_CONNECT_TIMEOUT_SECONDS": "0"},
        {"DB_PORT": "not-a-port"},
        {"DB_PORT": "70000"},
    ],
)
def test_invalid_values_raise(overrides):
    with pytest.raises(ConfigError) as info:
        load_settings({**DB_PARTS, **overrides})
    assert SECRET_PASSWORD not in str(info.value)


NON_FINITE_VALUES = ["nan", "NaN", "inf", "+inf", "-inf", "Infinity", "-Infinity", "1e309", "-1e309"]


@pytest.mark.parametrize("name", ["DB_RETRY_BASE_SECONDS", "DB_RETRY_MAX_SECONDS"])
@pytest.mark.parametrize("value", NON_FINITE_VALUES)
def test_non_finite_retry_settings_are_rejected(name, value):
    with pytest.raises(ConfigError) as info:
        load_settings({**DB_PARTS, name: value})
    assert str(info.value) == f"{name} must be a finite number"
    assert SECRET_PASSWORD not in str(info.value)


@pytest.mark.parametrize("name", ["DB_RETRY_BASE_SECONDS", "DB_RETRY_MAX_SECONDS"])
@pytest.mark.parametrize("value", ["0.001", "1", "30", "1e3"])
def test_finite_positive_retry_settings_still_accepted(name, value):
    other = "DB_RETRY_MAX_SECONDS" if name == "DB_RETRY_BASE_SECONDS" else "DB_RETRY_BASE_SECONDS"
    # keep base <= max so only the value under test matters
    pair = {name: value, other: "1e4" if other == "DB_RETRY_MAX_SECONDS" else "0.001"}
    settings = load_settings({**DB_PARTS, **pair})
    assert getattr(settings, name.lower().removeprefix("db_")) == float(value)


@pytest.mark.parametrize("port", ["1", "65535"])
def test_db_port_boundaries_accepted(port):
    settings = load_settings({**DB_PARTS, "DB_PORT": port})
    assert conninfo_to_dict(settings.database_url)["port"] == port


@pytest.mark.parametrize(("port", "message"), [("0", "DB_PORT must be >= 1"), ("65536", "DB_PORT must be <= 65535")])
def test_db_port_boundaries_rejected(port, message):
    with pytest.raises(ConfigError) as info:
        load_settings({**DB_PARTS, "DB_PORT": port})
    assert str(info.value) == message


def test_repr_never_contains_connection_string_or_password():
    s = load_settings({**DB_PARTS, "DATABASE_URL": f"postgresql://u:{SECRET_PASSWORD}@h:5432/d"})
    assert SECRET_PASSWORD not in repr(s)
    assert "database_url" not in repr(s)


# ---------------------------------------------------------------------------
# DATABASE_URL validation
# ---------------------------------------------------------------------------

LEAKY_USER = "leaky_user_q7"
LEAKY_HOST = "leaky-host.example"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://loantrack_user:pw@postgres:5432/loantrack",
        "postgres://loantrack_user:pw@localhost:5433/loantrack",
        "postgresql://u:p%40ss@h:5432/d?sslmode=disable",  # percent-encoded '@' is valid
        "host=postgres port=5432 dbname=loantrack user=loantrack_user password=pw",  # key=value form
        "postgresql://u:p@host1:5432,host2:5433/d",  # multi-host: comma-separated ports
        "postgresql://u:p@postgres/loantrack",  # no port: libpq default 5432
    ],
)
def test_valid_database_urls_are_accepted_unchanged(url):
    assert load_settings({"DATABASE_URL": url}).database_url == url


MALFORMED_URLS = [
    f"postgresql://{LEAKY_USER}:{SECRET_PASSWORD}%zz@{LEAKY_HOST}:5432/db",  # bad percent escape
    f"postgresql://{LEAKY_USER}:{SECRET_PASSWORD}@{LEAKY_HOST}:notaport/db",  # non-numeric port
    f"postgresql://{LEAKY_USER}:{SECRET_PASSWORD}@{LEAKY_HOST}:70000/db",  # port out of range
    f"{LEAKY_USER} {SECRET_PASSWORD} {LEAKY_HOST}",  # neither URI nor key=value
    f"host={LEAKY_HOST} user={LEAKY_USER} password='{SECRET_PASSWORD}",  # unterminated quote
    f"mysql://{LEAKY_USER}:{SECRET_PASSWORD}@{LEAKY_HOST}/db",  # not a PostgreSQL URL
]


@pytest.mark.parametrize("url", MALFORMED_URLS, ids=range(len(MALFORMED_URLS)))
def test_malformed_database_url_raises_safe_config_error(url):
    with pytest.raises(ConfigError) as info:
        load_settings({**DB_PARTS, "DATABASE_URL": url})  # parts present: URL still wins and is validated
    error = info.value
    assert str(error) == "DATABASE_URL is not a valid PostgreSQL connection string"
    # Nothing from psycopg's parse error (which quotes the URL) travels with it.
    assert error.__cause__ is None and error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for secret in (SECRET_PASSWORD, LEAKY_USER, LEAKY_HOST, url):
        assert secret not in rendered


def test_malformed_database_url_fails_app_startup_without_logging_secrets(monkeypatch, caplog, capsys):
    from app.main import create_app

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("DATABASE_URL", MALFORMED_URLS[0])
    with pytest.raises(ConfigError):
        create_app()  # reads os.environ, as `uvicorn app.main:app` does at import
    captured = capsys.readouterr()
    for secret in (SECRET_PASSWORD, LEAKY_USER, LEAKY_HOST):
        assert secret not in caplog.text
        assert secret not in captured.out and secret not in captured.err


def test_parts_path_unaffected_by_url_validation():
    # DATABASE_URL empty -> DB_HOST/DB_PORT/POSTGRES_* path, exactly as before.
    settings = load_settings({**DB_PARTS, "DATABASE_URL": ""})
    assert conninfo_to_dict(settings.database_url)["host"] == "db.internal.example"


def test_logging_is_info_to_stdout_with_timestamps():
    configure_logging()
    app_logger = logging.getLogger("loantrack")
    assert app_logger.level == logging.INFO
    handlers = [h for h in app_logger.handlers if isinstance(h, logging.StreamHandler)]
    assert handlers, "loantrack logger must have a stream handler"
    record = logging.LogRecord("loantrack.db", logging.INFO, __file__, 1, "hello", None, None)
    line = handlers[0].format(record)
    # e.g. 2026-11-27T10:00:00+0000 INFO loantrack.db hello
    assert line.split(" ")[0][:4].isdigit() and "T" in line.split(" ")[0]
    assert line.endswith("INFO loantrack.db hello")
    # psycopg's own connection-error warnings (which include host/user/db) are silenced.
    assert logging.getLogger("psycopg").level == logging.ERROR
