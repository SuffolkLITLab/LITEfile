from copy import deepcopy

import pytest
from django.core.exceptions import ImproperlyConfigured

from efile.database_pool import configure_connection_pool


@pytest.fixture
def database(monkeypatch):
    monkeypatch.delenv("DB_POOL_MAX_SIZE", raising=False)
    monkeypatch.delenv("DB_POOL_TIMEOUT", raising=False)
    return {"ENGINE": "django.db.backends.postgresql", "CONN_MAX_AGE": 0, "OPTIONS": {"sslmode": "require"}}


def test_pool_is_opt_in(database):
    before = deepcopy(database)
    configure_connection_pool(database)
    assert database == before


def test_pool_preserves_tls_and_uses_bounded_lazy_connections(database, monkeypatch):
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "8")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "45")
    configure_connection_pool(database)
    assert database["OPTIONS"] == {
        "sslmode": "require",
        "pool": {"min_size": 0, "max_size": 8, "timeout": 45},
    }
    assert database["CONN_MAX_AGE"] == 0
    assert database["CONN_HEALTH_CHECKS"] is True


@pytest.mark.parametrize(
    "name,value",
    [
        ("DB_POOL_MAX_SIZE", "-1"),
        ("DB_POOL_MAX_SIZE", "1.5"),
        ("DB_POOL_TIMEOUT", "0"),
        ("DB_POOL_TIMEOUT", "-1"),
        ("DB_POOL_TIMEOUT", "nan"),
        ("DB_POOL_TIMEOUT", "inf"),
    ],
)
def test_invalid_pool_limits_fail_at_startup(database, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ImproperlyConfigured):
        configure_connection_pool(database)


def test_pool_cannot_enable_persistent_asgi_connections(database, monkeypatch):
    database["CONN_MAX_AGE"] = 60
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "8")
    with pytest.raises(ImproperlyConfigured, match="CONN_MAX_AGE=0"):
        configure_connection_pool(database)


def test_sqlite_collectstatic_never_receives_pool_options(monkeypatch):
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "8")
    database = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    before = deepcopy(database)
    configure_connection_pool(database)
    assert database == before


@pytest.mark.django_db(transaction=True)
def test_pooled_requests_do_not_leak_rolled_back_writes_or_transactions(django_user_model):
    from django.db import connection, transaction

    if connection.vendor != "postgresql" or not connection.settings_dict["OPTIONS"].get("pool"):
        pytest.skip("Requires the isolated PostgreSQL pool integration configuration")
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with transaction.atomic():
            django_user_model.objects.create(username="pool-rollback-sentinel")
            raise RuntimeError("rollback sentinel")
    connection.close()  # The request boundary returns the connection to the pool.
    assert connection.get_autocommit()
    assert not django_user_model.objects.filter(username="pool-rollback-sentinel").exists()
    django_user_model.objects.create(username="pool-commit-sentinel")
    connection.close()
    assert django_user_model.objects.filter(username="pool-commit-sentinel").exists()
    assert not connection.in_atomic_block


@pytest.mark.django_db(transaction=True)
def test_pool_recovers_after_an_idle_connection_is_terminated():
    import psycopg
    from django.db import connection

    if connection.vendor != "postgresql" or not connection.settings_dict["OPTIONS"].get("pool"):
        pytest.skip("Requires the isolated PostgreSQL pool integration configuration")
    # Only terminate this test's own idle connection, in the disposable test DB.
    assert str(connection.settings_dict["NAME"]).startswith("test_")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        pid = cursor.fetchone()[0]
    connection.close()
    with psycopg.connect(**connection.get_connection_params(), autocommit=True) as control:
        control.execute("SELECT pg_terminate_backend(%s)", [pid])
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)
