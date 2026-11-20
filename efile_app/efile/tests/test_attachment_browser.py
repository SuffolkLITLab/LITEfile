"""Opt-in browser regression: ATTACHMENT_GROUP_BROWSER_TESTS=1 uv run pytest -s efile/tests/test_attachment_browser.py."""

import json
import os
import subprocess

import pytest
from django.conf import settings

from efile.tests.test_document_prep import document_draft
from efile.tests.test_filing_groups import grouped

__all__ = ["document_draft", "grouped"]


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not os.getenv("ATTACHMENT_GROUP_BROWSER_TESTS"), reason="Opt-in: requires Chromium")
def test_attachment_choices_and_parent_code_changes(live_server, client, grouped, tmp_path):
    draft, lead, child = grouped
    child.attached_to = None
    child.filing_component_code = "332"
    child.courtesy_copy_email = "test@example.org"
    child.save()
    config = tmp_path / "attachment-browser.json"
    config.write_text(
        json.dumps(
            {
                "baseUrl": live_server.url,
                "cookieName": settings.SESSION_COOKIE_NAME,
                "cookie": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "draft": draft.pk,
                "lead": lead.pk,
                "child": child.pk,
            }
        )
    )
    result = subprocess.run(
        ["node", "tests/attachment-groups-browser.js", str(config)],
        cwd=settings.BASE_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    child.refresh_from_db()
    lead.refresh_from_db()
    assert child.attached_to_id == lead.pk
    assert child.filing_type_code == lead.filing_type_code == "exhibit"
    assert child.filing_component_code == "331"
    assert not child.courtesy_copy_email
