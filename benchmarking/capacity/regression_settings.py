"""Isolated PostgreSQL/browser regressions; fixed loopback services, no cloud credentials."""

import dj_database_url

from efile.settings_dev import *  # noqa: F401,F403
from efile.database_pool import configure_connection_pool

DATABASES = {
    "default": dj_database_url.parse(
        "postgresql://capacity:local-capacity-only@127.0.0.1:8543/capacity"
    )
}
DATABASES["default"]["TEST"] = {"NAME": "test_capacity_regression"}
configure_connection_pool(DATABASES["default"])
GOTENBERG_URL = "http://127.0.0.1:8300"
GOTENBERG_USERNAME = ""
GOTENBERG_PASSWORD = ""
EFSP_URL = "http://127.0.0.1:9"
# Some submission tests generate signed URLs locally without fetching objects.
AWS_ACCESS_KEY_ID = "test"
AWS_SECRET_ACCESS_KEY = "test"
AWS_SESSION_TOKEN = ""
AWS_S3_BUCKET_NAME = "synthetic"
AWS_S3_REGION_NAME = "us-east-1"
AWS_S3_ENDPOINT_URL = "http://127.0.0.1:9"
