import os

import dj_database_url

from efile.database_pool import configure_connection_pool
from efile.settings_base import *  # noqa: F401,F403

# Import specific names with aliases to avoid F405 from star import usage
from efile.settings_base import BASE_DIR as BASE_SETTINGS_DIR  # noqa: F401
from efile.settings_base import DATABASES as BASE_DATABASES
from efile.settings_base import MIDDLEWARE as BASE_MIDDLEWARE

# Bind DATABASES explicitly to avoid F405 and make linter aware of the symbol
DATABASES = BASE_DATABASES

DEBUG = False
ALLOWED_HOSTS = ["litefile-staging.fly.dev"]
CSRF_TRUSTED_ORIGINS = ["https://litefile-staging.fly.dev"]

# Security hardening suitable for staging
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY or SECRET_KEY.startswith("django-insecure-"):
    raise RuntimeError("DJANGO_SECRET_KEY must be set to a strong value in staging")

SECURE_SSL_REDIRECT = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HSTS with short window for staging
SECURE_HSTS_SECONDS = 86400  # 1 day
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

# Static files (WhiteNoise)
# Collect static into a dedicated directory and serve with WhiteNoise
STATIC_ROOT = BASE_SETTINGS_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Build MIDDLEWARE from base and insert WhiteNoise right after SecurityMiddleware
MIDDLEWARE = list(BASE_MIDDLEWARE)
try:
    security_index = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
except ValueError:
    security_index = -1
insert_at = security_index + 1 if security_index >= 0 else 0
MIDDLEWARE.insert(insert_at, "whitenoise.middleware.WhiteNoiseMiddleware")

# Verbose logging to console for staging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"staff_redaction": {"()": "efile.staff_logging.StaffLogRedactionFilter"}},
    "formatters": {
        "verbose": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["staff_redaction"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "gunicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "whitenoise": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
# NOTE: Papertrail integration has not been tested, but an example configuration is provided below.
# This would be used in place of, or in addition to, the console handler above, by updating the loggers
# to reference the new handler (or defining an inheritable handler and using propagate=True).
# LOGGING["handlers"]["papertrail"] = {
#     "class": "logging.handlers.SysLogHandler",
#     "address": ("<PAPERTRAIL_HOST>", <PAPERTRAIL_PORT>),
#     "formatter": "verbose",
# }
# LOGGING["root"]["handlers"].append("papertrail")

# ASGI requires short-lived connections. Configure any bounded pool separately.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set for staging environment")

DATABASES["default"] = dj_database_url.config(
    default=DATABASE_URL,
    conn_max_age=0,
    ssl_require=True,
)

# Explicit opt-in; applies per process, including background worker processes.
configure_connection_pool(DATABASES["default"])
