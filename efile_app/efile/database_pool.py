"""Optional process-local PostgreSQL pooling for deployed ASGI applications."""

import math
import os

from django.core.exceptions import ImproperlyConfigured


def configure_connection_pool(database):
    """Keep pooling opt-in; size is per process, not per deployment.

    SQLite is used during image builds and needs no pool. Background workers
    also load deployed settings, so the deployment budget must include them.
    """
    if database["ENGINE"] != "django.db.backends.postgresql":
        return
    try:
        size = int(os.getenv("DB_POOL_MAX_SIZE", "0"))
        timeout = float(os.getenv("DB_POOL_TIMEOUT", "30"))
    except ValueError as error:
        raise ImproperlyConfigured("DB_POOL_MAX_SIZE and DB_POOL_TIMEOUT must be numeric") from error
    if size < 0 or not math.isfinite(timeout) or timeout <= 0:
        raise ImproperlyConfigured("DB_POOL_MAX_SIZE must be nonnegative and DB_POOL_TIMEOUT positive and finite")
    if not size:
        return
    if database.get("CONN_MAX_AGE", 0) != 0:
        raise ImproperlyConfigured("Database pooling requires CONN_MAX_AGE=0")
    database.setdefault("OPTIONS", {})["pool"] = {
        "min_size": 0,
        "max_size": size,
        "timeout": timeout,
    }
    database["CONN_HEALTH_CHECKS"] = True
