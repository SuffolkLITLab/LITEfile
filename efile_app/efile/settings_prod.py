import os

import dj_database_url

from efile.settings_base import *  # noqa: F401,F403

# Import specific names with aliases to avoid F405 from star import usage
from efile.settings_base import BASE_DIR as BASE_SETTINGS_DIR
from efile.settings_base import DATABASES as BASE_DATABASES
from efile.settings_base import MIDDLEWARE as BASE_MIDDLEWARE

# Bind DATABASES explicitly to avoid F405 and make linter aware of the symbol
DATABASES = BASE_DATABASES

DEBUG = False
ALLOWED_HOSTS = ["app.litefile.org", "litefile-prod.fly.dev"]
CSRF_TRUSTED_ORIGINS = ["https://app.litefile.org", "https://litefile-prod.fly.dev"]

# Static files (WhiteNoise), mirroring settings_staging.
#
# ManifestStaticFilesStorage content-hashes each filename (payment.js ->
# payment.4a3f9c2b.js), so a changed asset gets a new URL and an unchanged one
# keeps its cache entry. This is why templates carry no manual ?v= cache-buster.
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
MIDDLEWARE.insert(security_index + 1 if security_index >= 0 else 0, "whitenoise.middleware.WhiteNoiseMiddleware")

# Security hardening
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY or SECRET_KEY.startswith("django-insecure-"):
    raise RuntimeError("DJANGO_SECRET_KEY must be set to a strong value in production")

SECURE_SSL_REDIRECT = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# ASGI requires short-lived connections. Configure any bounded pool separately.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set for production environment")

DATABASES["default"] = dj_database_url.config(
    default=DATABASE_URL,
    conn_max_age=0,
    ssl_require=True,
)
