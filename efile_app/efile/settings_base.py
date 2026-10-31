import json
import os
import re
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
# Dev fallback; staging/prod must override via env in their settings modules.
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-r1o&rohs_$%i1)u-8amhsu)o!z4ply+x^*_ht&-jav0h6rnpdo")

# SECURITY WARNING: don't run with debug turned on in production!
# Override in env-specific settings
DEBUG = True

# Override in env-specific settings
_configured_allowed_hosts = os.getenv("DJANGO_ALLOWED_HOSTS", "")
ALLOWED_HOSTS: list[str] = [host.strip() for host in _configured_allowed_hosts.split(",") if host.strip()]

# Override in env-specific settings
CSRF_TRUSTED_ORIGINS: list[str] = []

AUTH_USER_MODEL = "efile.UserProfile"

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "efile",
    "efile.templatetags.md_to_html",
    "crosswalk_review",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Activates the filer's language for the request. Inert while LANGUAGES
    # lists only English; it is here so turning on a second language is a
    # settings change rather than a plumbing change.
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "efile.staff_security.StaffIsolationMiddleware",
    "efile.middleware.JurisdictionSessionMiddleware",
    "efile.middleware.DraftIdentityMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "efile.middleware.NoCacheHTMLMiddleware",
]

ROOT_URLCONF = "efile.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "efile/templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "efile.context_processors.jurisdiction_context",
                "efile.services.draft_urls.browser_draft_context",
            ],
        },
    },
]

WSGI_APPLICATION = "efile.wsgi.application"

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    # Shared by every worker and machine, for results that are slow to compute.
    # Migration 0043 creates its table.
    "shared": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "efile_shared_cache",
        "OPTIONS": {"MAX_ENTRIES": 2000},
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "efile.authentication.SuffolkEFileBackend",  # for logging users in with Tyler
    "django.contrib.auth.backends.ModelBackend",
]

SESSION_COOKIE_AGE = 86400  # 24 hours
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Translations live in efile/locale/. Add ("es", _("Spanish")) here once
# efile/locale/es/LC_MESSAGES/django.po is translated and compiled -- listing a
# language before it has a catalog gives a Spanish-speaking filer Django's own
# Spanish form errors around our untranslated English copy.
LANGUAGES = [
    ("en", "English"),
]
LOCALE_PATHS = [BASE_DIR / "efile" / "locale"]

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "efile/static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SUFFOLK_EFILE_API_KEY = os.getenv("SUFFOLK_EFILE_API_KEY", "")

AI_PROVIDER = os.getenv("AI_PROVIDER", "openai")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", None)
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1/")

# Base URL for EFSP-compatible APIs (Suffolk LIT Lab by default)
# Override per environment with EFSP_URL env var
EFSP_URL = os.getenv("EFSP_URL", "https://efile-test.suffolklitlab.org")
# Where filing codes come from: "database" copies them straight from the EFSP
# proxy's codes database (read only); "bulk" and "legacy" use its HTTP API.
FILING_CODE_SYNC_MODE = os.getenv("FILING_CODE_SYNC_MODE", "database")
# The proxy's Postgres codes database. LITEfile only ever reads it, inside
# READ ONLY transactions; a read-only database role is still recommended.
EFSP_CODES_DATABASE_URL = os.getenv("EFSP_CODES_DATABASE_URL", "")
# When the code index worker copies codes each day, in FILING_CODE_SYNC_TIMEZONE.
# Set it well after the proxy's own daily Tyler code update (production 02:13,
# test 19:35 Eastern) so that day's codes are in place.
FILING_CODE_SYNC_TIME = os.getenv("FILING_CODE_SYNC_TIME", "04:30")
FILING_CODE_SYNC_TIMEZONE = os.getenv("FILING_CODE_SYNC_TIMEZONE", "America/New_York")
EFILE_SUBMISSION_CONNECT_TIMEOUT_SECONDS = int(os.getenv("EFILE_SUBMISSION_CONNECT_TIMEOUT_SECONDS", "10"))
EFILE_SUBMISSION_READ_TIMEOUT_SECONDS = int(os.getenv("EFILE_SUBMISSION_READ_TIMEOUT_SECONDS", "300"))

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN", "")
AWS_S3_BUCKET_NAME = os.getenv("AWS_S3_BUCKET_NAME", "")
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "us-east-1")
AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL", None)

# The local crosswalk reviewer needs a separate PDF corpus. Keep its routes off
# in deployed filing apps while that corpus is not provisioned outside the image.
CROSSWALK_REVIEW_ENABLED = False

# Private staff URL: intentionally absent from public menus and sitemaps.
LITEFILE_STAFF_PATH = os.getenv("LITEFILE_STAFF_PATH", "staff-7c83f0a2").strip("/")
if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{11,79}", LITEFILE_STAFF_PATH):
    raise ValueError(
        "LITEFILE_STAFF_PATH must be a private URL segment of 12–80 letters, digits, underscores or hyphens."
    )
LITEFILE_STAFF_SESSION_SECONDS = 900
LITEFILE_ANALYTICS_ENABLED = os.getenv("LITEFILE_ANALYTICS_ENABLED", "false").lower() == "true"
LITEFILE_PRIVACY_DELETION_ENABLED = os.getenv("LITEFILE_PRIVACY_DELETION_ENABLED", "false").lower() == "true"
LITEFILE_ANALYTICS_EXPORT_ENABLED = os.getenv("LITEFILE_ANALYTICS_EXPORT_ENABLED", "false").lower() == "true"
LITEFILE_ANALYTICS_MIN_CONTRIBUTORS = int(os.getenv("LITEFILE_ANALYTICS_MIN_CONTRIBUTORS", "5"))
LITEFILE_STAFF_REQUEST_RETENTION_DAYS = int(os.getenv("LITEFILE_STAFF_REQUEST_RETENTION_DAYS", "90"))

# File Upload Settings
DOCUMENT_EXTRACTION_TIMEOUT_SECONDS = int(os.getenv("DOCUMENT_EXTRACTION_TIMEOUT_SECONDS", "600"))
DOCUMENT_EXTRACTION_MEMORY_MB = int(os.getenv("DOCUMENT_EXTRACTION_MEMORY_MB", "768"))
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_FILE_TYPES = [".pdf", ".doc", ".docx"]

# Gotenberg document conversion service
GOTENBERG_URL = os.getenv("GOTENBERG_URL", "")
GOTENBERG_USERNAME = os.getenv("GOTENBERG_USERNAME", "")
GOTENBERG_PASSWORD = os.getenv("GOTENBERG_PASSWORD", "")
DOCUMENT_PREPARATION_TIMEOUT_SECONDS = int(os.getenv("DOCUMENT_PREPARATION_TIMEOUT_SECONDS", "45"))
# Analyze only the front of a filing. Exhibits and discovery can make a PDF
# hundreds of pages long, while the caption and filing details normally appear
# near the beginning.
DOCUMENT_EXTRACTION_MAX_PAGES = int(os.getenv("DOCUMENT_EXTRACTION_MAX_PAGES", "20"))
DOCUMENT_EXTRACTION_MAX_TEXT_CHARS = int(os.getenv("DOCUMENT_EXTRACTION_MAX_TEXT_CHARS", "100000"))
DOCUMENT_EXTRACTION_MAX_ATTEMPTS = int(os.getenv("DOCUMENT_EXTRACTION_MAX_ATTEMPTS", "3"))
DOCUMENT_CLASSIFICATION_SOURCE_PAGES = int(os.getenv("DOCUMENT_CLASSIFICATION_SOURCE_PAGES", "3"))
# The single-reader path remains available as an explicit rollback.
DOCUMENT_EVIDENCE_STRATEGY = os.getenv(
    "DOCUMENT_EVIDENCE_STRATEGY", "bedrock_ensemble" if AI_PROVIDER == "bedrock" else "terra_ensemble"
)
DOCUMENT_ENSEMBLE_READERS_BASE_URL = os.getenv("DOCUMENT_ENSEMBLE_READERS_BASE_URL", "")
DOCUMENT_ENSEMBLE_READERS_API_KEY = os.getenv("DOCUMENT_ENSEMBLE_READERS_API_KEY", "")
DOCUMENT_ENSEMBLE_TERRA_BASE_URL = os.getenv("DOCUMENT_ENSEMBLE_TERRA_BASE_URL", "")
DOCUMENT_ENSEMBLE_TERRA_API_KEY = os.getenv("DOCUMENT_ENSEMBLE_TERRA_API_KEY", "")
# Dated estimates from the October 30 corpus study; not billing guarantees.
DOCUMENT_ENSEMBLE_MAX_CALLS = int(os.getenv("DOCUMENT_ENSEMBLE_MAX_CALLS", "32"))
DOCUMENT_ENSEMBLE_BEDROCK_ROLE_ARN = os.getenv("DOCUMENT_ENSEMBLE_BEDROCK_ROLE_ARN", "")
DOCUMENT_ENSEMBLE_BEDROCK_REGION = os.getenv("DOCUMENT_ENSEMBLE_BEDROCK_REGION", "us-east-1")
DOCUMENT_ENSEMBLE_MODELS = {
    role: {
        "deployment": os.getenv(f"DOCUMENT_ENSEMBLE_{role.upper()}_MODEL", model),
        "auth": os.getenv(f"DOCUMENT_ENSEMBLE_{role.upper()}_AUTH", "api_key"),
        # Optional per-role routing permits an explicit mixed-provider rollout.
        "base_url": os.getenv(f"DOCUMENT_ENSEMBLE_{role.upper()}_BASE_URL", ""),
        "api_key": os.getenv(f"DOCUMENT_ENSEMBLE_{role.upper()}_API_KEY", ""),
        "output_parameter": parameter,
        "reasoning_effort": reasoning,
        "input_usd_per_million": float(os.getenv(f"DOCUMENT_ENSEMBLE_{role.upper()}_INPUT_RATE", str(input_rate))),
        "output_usd_per_million": float(os.getenv(f"DOCUMENT_ENSEMBLE_{role.upper()}_OUTPUT_RATE", str(output_rate))),
    }
    for role, model, parameter, reasoning, input_rate, output_rate in [
        ("deepseek", "deepseek-v4.1-flash", "max_tokens", None, 0.30, 1.20),
        ("luna", "gpt-6-luna", "max_completion_tokens", "none", 0.10, 0.50),
        ("glm", "glm-5.3-flash", "max_tokens", None, 0.188, 0.625),
        ("terra", "gpt-6.1-terra", "max_completion_tokens", "low", 1.10, 5.50),
    ]
}
# AWS-only deployment: no inherited API keys or third-party endpoints.
if AI_PROVIDER == "bedrock":
    DOCUMENT_ENSEMBLE_MODELS = {
        role: {
            "deployment": model,
            "auth": "fly_oidc",
            "base_url": f"https://bedrock-runtime.{DOCUMENT_ENSEMBLE_BEDROCK_REGION}.amazonaws.com/openai/v1/",
            "api_key": "",
            "api": "anthropic" if role == "haiku" else "openai",
            "output_parameter": "max_tokens" if role == "haiku" else "max_completion_tokens",
            "reasoning_effort": None if role == "haiku" else ("none" if role == "luna" else "low"),
            "input_usd_per_million": input_rate,
            "output_usd_per_million": output_rate,
        }
        for role, model, input_rate, output_rate in [
            ("luna", "us.openai.gpt-6-luna", 0.11, 0.55),
            ("haiku", "us.anthropic.claude-haiku-5-5", 0.11, 0.55),
            ("terra", "us.openai.gpt-6.1-terra", 1.10, 5.50),
        ]
    }

DOCUMENT_EVIDENCE_MODEL = os.getenv("DOCUMENT_EVIDENCE_MODEL", "")
DOCUMENT_CLASSIFICATION_MODEL = os.getenv("DOCUMENT_CLASSIFICATION_MODEL", "")
FORM_CODE_CROSSWALK_PATH = Path(
    os.getenv("FORM_CODE_CROSSWALK_PATH", BASE_DIR / "efile" / "data" / "form_code_crosswalk.json")
)
# Local PDFs and their form_registry.json are used by the crosswalk review UI.
# The directory is intentionally outside the application package because the
# downloaded dataset is local/ignored rather than part of the deploy image.
CROSSWALK_REVIEW_FORMS_ROOT = Path(os.getenv("CROSSWALK_REVIEW_FORMS_ROOT", BASE_DIR.parent / "court_forms"))

# Logging configuration
# Uses standard Python logging. Control the app logger level via DJANGO_LOG_LEVEL env var.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"staff_redaction": {"()": "efile.staff_logging.StaffLogRedactionFilter"}},
    "formatters": {
        "simple": {
            "format": "[%(levelname)s] %(asctime)s %(name)s: %(message)s",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "filters": ["staff_redaction"],
        }
    },
    "loggers": {
        # Django's own logs
        "django": {
            "handlers": ["console"],
            "level": os.getenv("DJANGO_DJANGO_LOG_LEVEL", "INFO"),
            "propagate": True,
        },
        # App-specific logger
        "efile": {
            "handlers": ["console"],
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

# Per-source bearer credentials; never send these to the browser.
# {"vermont-rfa": {"token": "...", "jurisdictions": ["vermont"],
#                  "return_origins": ["https://interviews.example.org"]}}
LITEFILE_HANDOFF_SOURCES = json.loads(os.getenv("LITEFILE_HANDOFF_SOURCES", "{}"))
LITEFILE_HANDOFF_TOKEN_MAX_AGE = 86400
