import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Refuse to be the settings module on a deployed host.
#
# `efile/settings.py` is a bare re-export of this module, and manage.py, wsgi.py
# and asgi.py all `setdefault("DJANGO_SETTINGS_MODULE", "efile.settings")`. So a
# deploy that loses DJANGO_SETTINGS_MODULE -- an edit to fly.toml's [env] block,
# a new machine started without it -- silently falls back to *development*
# settings: DEBUG=True, and EFSP_TEST_DOCUMENT_URL live, which would file
# stand-in PDFs against a real court. Every other guard on that setting keys off
# DEBUG, so none of them would fire in exactly this case.
#
# Fly always sets FLY_APP_NAME in the runtime environment, so its presence means
# "deployed" regardless of what DJANGO_SETTINGS_MODULE says. Failing at import
# turns a silent misconfiguration into a boot failure. Deliberately no override
# env var: an escape hatch here is the same footgun again.
if os.getenv("FLY_APP_NAME"):
    raise ImproperlyConfigured(
        "efile.settings_dev was loaded on a deployed host (FLY_APP_NAME="
        f"{os.environ['FLY_APP_NAME']!r}). Development settings enable DEBUG and the "
        "EFSP stand-in document. Set DJANGO_SETTINGS_MODULE to efile.settings_staging "
        "or efile.settings_prod."
    )

# Determine BASE_DIR without importing base to load .env first
_BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env at the project base directory BEFORE base import
load_dotenv(dotenv_path=_BASE_DIR / ".env", override=False)

from efile.settings_base import *  # noqa: E402,F401,F403
from efile.settings_base import ALLOWED_HOSTS as BASE_ALLOWED_HOSTS  # noqa: E402
from efile.settings_base import DATABASES as BASE_DATABASES  # noqa: E402
from efile.settings_base import INSTALLED_APPS as BASE_INSTALLED_APPS  # noqa: E402
from efile.settings_base import MIDDLEWARE as BASE_MIDDLEWARE  # noqa: E402

CROSSWALK_REVIEW_ENABLED = True

# Bind DATABASES explicitly to avoid F405 and make linter aware of the symbol
DATABASES = BASE_DATABASES

DEBUG = True
SHOW_BUILD_COMMIT = True
ALLOWED_HOSTS = BASE_ALLOWED_HOSTS or ["localhost", "127.0.0.1", ".localhost", "[::1]", "testserver"]

# Static files: serve dev assets through WhiteNoise, as staging and production do.
#
# `runserver`'s own static handler sends Last-Modified and an ETag but no
# Cache-Control, which leaves the browser free to guess a freshness lifetime
# (Chrome: a fraction of the file's age) and reuse a script for hours without
# revalidating. A JS change then reaches the tests and never reaches the browser
# -- which is how an upload page shipped a validation check that the running app
# did not have, and let a filing go out with no filing type selected.
#
# ManifestStaticFilesStorage covers the deployed environments by content-hashing
# each filename, so the fix belongs here rather than in a ?v= query string in the
# templates: a hand-maintained cache-buster only helps the files someone
# remembered to bump.
#
# `whitenoise.runserver_nostatic` must precede django.contrib.staticfiles to stop
# runserver from installing its handler ahead of the middleware. USE_FINDERS
# serves straight from STATICFILES_DIRS (no collectstatic in dev), AUTOREFRESH
# re-stats each file per request, and MAX_AGE=0 makes the browser revalidate
# every time.
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *BASE_INSTALLED_APPS]
MIDDLEWARE = list(BASE_MIDDLEWARE)
MIDDLEWARE.insert(
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
    "whitenoise.middleware.WhiteNoiseMiddleware",
)
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True
WHITENOISE_MAX_AGE = 0

# Opt-in: send this URL to the EFSP proxy as every document's `data_url` instead
# of the real S3 URL.
#
# The proxy downloads each `data_url` itself -- for a fee quote as well as for a
# filing -- and only accepts http(s), so a LocalStack URL is unreachable to it.
# Setting this to any publicly readable PDF lets fee quotes and submissions run
# end-to-end from a laptop with no ingress, tunnel, or public bucket.
#
# Uploads still go to S3/LocalStack for real and drafts still store the real keys
# and URLs; only the URL handed to the proxy changes. Defined here rather than in
# settings_base so no environment variable can enable it outside development.
EFSP_TEST_DOCUMENT_URL = os.getenv("EFSP_TEST_DOCUMENT_URL", "").strip()
# Extra filing guidance for local validation, such as
# ../testing/filing-guidance-demo.yaml. Development only, like the setting above,
# so demonstration messages can never be shown to filers on a deployed site.
FILING_GUIDANCE_DEMO_FILE = os.getenv("FILING_GUIDANCE_DEMO_FILE", "").strip()
CSRF_TRUSTED_ORIGINS = [
    "http://localhost",
    "http://127.0.0.1",
    "http://::1",
    "http://testserver",
]

# If a DATABASE_URL is provided locally, prefer Postgres over SQLite
_DEV_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if _DEV_DATABASE_URL:
    DATABASES["default"] = dj_database_url.config(
        default=_DEV_DATABASE_URL,
        conn_max_age=0,  # no pooling for dev; immediate close on request end
        ssl_require=False,
    )

if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    # Uploads and background workers read before writing. SQLite ignores
    # select_for_update(); reserve the writer at BEGIN so those transactions
    # wait instead of failing when upgrading a read lock after storage/network IO.
    DATABASES["default"].setdefault("OPTIONS", {}).update(transaction_mode="IMMEDIATE", timeout=60)
