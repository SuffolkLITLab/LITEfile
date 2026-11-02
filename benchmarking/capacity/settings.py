"""Production settings with explicit, isolated local transport substitutions."""

import os

from efile.settings_prod import *  # noqa: F403,F401

ALLOWED_HOSTS = ["127.0.0.1", "localhost", "web"]
SECURE_SSL_REDIRECT = False
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
DATABASES["default"].setdefault("OPTIONS", {})["sslmode"] = "disable"  # noqa: F405
EFSP_URL = "http://upstream:8080"
AWS_ACCESS_KEY_ID = "test"
AWS_SECRET_ACCESS_KEY = "test"
AWS_S3_BUCKET_NAME = "capacity"
AWS_S3_ENDPOINT_URL = "http://storage:4566"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Only used by the explicit experimental overlay; baseline preserves production.

if os.getenv("CAPACITY_POOL_SIZE"):
    DATABASES["default"]["OPTIONS"]["pool"] = {  # noqa: F405
        "min_size": 0,
        "max_size": int(os.environ["CAPACITY_POOL_SIZE"]),
        "timeout": 20,
    }
