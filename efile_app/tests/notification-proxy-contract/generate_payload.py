"""Generate deterministic LITEfile notification fields for the proxy contract check."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "efile.settings")

import django

django.setup()

from django.test import RequestFactory  # noqa: E402

from efile.services.submission_notifications import (  # noqa: E402
    PROXY_EMAIL_FIELDS_REQUIRED,
    submission_email_fields,
)
from efile.utils.config_loader import config_loader  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="JSON path for generated proxy fields")
    parser.add_argument("--jurisdiction", default="illinois")
    parser.add_argument("--host", default="testserver")
    parser.add_argument("--draft-id", default="314159")
    args = parser.parse_args()

    config = config_loader.load_jurisdiction_config(args.jurisdiction)
    if not isinstance(config, dict):
        parser.error(f"No configuration found for jurisdiction {args.jurisdiction!r}")
    config["submission_notifications"]["enabled"] = True
    config.setdefault("court_specific_requirements", {})["notification-contract-test-court"] = {
        "contact": {"name": "{{name}}", "url": "https://help.example.test/{{status}}"}
    }
    config.setdefault("fee_waiver", {})["source_url"] = "https://source.example.test/{{transaction_id}}"

    request = RequestFactory().get("/", HTTP_HOST=args.host)
    draft = SimpleNamespace(
        pk=args.draft_id,
        public_id=UUID("6b0f2a8e-6d0c-4f1e-9a51-0c2b9f3c1a7d"),
        selected_payment_account_type="WV",
        court_code="notification-contract-test-court",
        case_title="Jordan <script>bad</script> {{status}}",
        court_name="Test court {{name}}",
    )

    # Probe untrusted configuration values through the same interpolation path
    # used by live court contact names and URLs.
    with patch(
        "efile.services.submission_notifications.court_contact",
        return_value={"name": "{{name}}", "url": "https://help.example.test/{{status}}"},
    ):
        fields = submission_email_fields(request, draft, args.jurisdiction, config)

    if set(fields) != set(PROXY_EMAIL_FIELDS_REQUIRED):
        raise SystemExit(f"Expected proxy fields {sorted(PROXY_EMAIL_FIELDS_REQUIRED)}, got {sorted(fields)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fields": fields,
        # Subjects are passed to the provider verbatim, while bodies are rendered.
        "subject_probe": "Filing update {{ status }}",
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(fields)} proxy fields to {args.output}")


if __name__ == "__main__":
    main()
