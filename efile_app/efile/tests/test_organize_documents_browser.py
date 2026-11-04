"""Real-browser checks of how Organize documents handles slow, failed and late court lookups."""

import json
import os
import subprocess
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import Client, override_settings
from django.urls import reverse

from efile.models import FilingDraft
from efile.tests.helpers import loaded_case_snapshot, reviewed_document
from efile.tests.test_document_extractions import authorize
from efile.workflow import ExistingCase


# Every lookup is answered in the browser. Nothing here should reach a court.
@override_settings(EFSP_URL="http://127.0.0.1:9")
@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not os.getenv("CORE_FILING_UX_BROWSER_TESTS"), reason="Opt-in: requires Chromium")
def test_court_choice_lookups_in_browser(live_server, django_user_model, tmp_path):
    user = django_user_model.objects.create_user(username="organize-browser", tyler_jurisdiction="vermont")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="vermont",
        existing_case=ExistingCase.NEW,
        workflow_version=2,
        court_code="vt:family",
        case_category_code="family",
        case_category_name="Family",
        case_type_code="relief-from-abuse",
        case_type_name="Relief from Abuse",
    )
    reviewed_document(draft=draft, role="lead", name="complaint.pdf", s3_key="complaint.pdf")
    reviewed_document(draft=draft, role="supporting", name="affidavit.pdf", s3_key="affidavit.pdf", sort_order=0)
    client = Client()
    authorize(client, draft)

    existing_user = django_user_model.objects.create_user(username="organize-existing", tyler_jurisdiction="vermont")
    existing = FilingDraft.objects.create(
        user=existing_user,
        jurisdiction="vermont",
        existing_case=ExistingCase.EXISTING,
        workflow_version=2,
        court_code="vt:family",
        previous_case_id="case-1",
    )
    existing.existing_case_snapshot = loaded_case_snapshot(existing)
    existing.save()
    reviewed_document(draft=existing, role="lead", name="motion.pdf", s3_key="motion.pdf")
    reviewed_document(draft=existing, role="supporting", name="exhibit.pdf", s3_key="exhibit.pdf", sort_order=0)
    existing_client = Client()
    authorize(existing_client, existing)

    config = tmp_path / "organize.json"
    config.write_text(
        json.dumps(
            {
                "baseUrl": live_server.url,
                "cookie": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": reverse("organize_documents", kwargs={"jurisdiction": "vermont"}) + f"?draft={draft.pk}",
                "existingCase": {
                    "cookie": existing_client.cookies[settings.SESSION_COOKIE_NAME].value,
                    "url": reverse("organize_documents", kwargs={"jurisdiction": "vermont"}) + f"?draft={existing.pk}",
                },
            }
        )
    )
    with patch("efile.services.people.get_party_types", return_value=[]):
        result = subprocess.run(
            ["node", "tests/organize-documents-browser.js", str(config)],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
    assert result.returncode == 0, result.stdout + result.stderr
