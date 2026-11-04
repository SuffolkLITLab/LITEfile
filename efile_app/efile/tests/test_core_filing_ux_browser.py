"""Synthetic browser matrix for the existing-case portion of the core stack."""

import json
import os
import subprocess
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from efile.models import FilingDraft
from efile.tests.helpers import loaded_case_snapshot, reviewed_document
from efile.tests.test_document_extractions import authorize


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not os.getenv("CORE_FILING_UX_BROWSER_TESTS"), reason="Opt-in: requires Chromium")
def test_existing_case_browser_matrix(live_server, django_user_model, tmp_path):
    scenarios = []
    for name, jurisdiction, number, filing_guess, reject_guess in [
        ("modern", "massachusetts", "1448CV001026", "Complaint", False),
        ("legacy", "massachusetts", "98-1234", "Motion", False),
        ("vermont", "vermont", "24-CV-00123", "Motion", True),
    ]:
        user = django_user_model.objects.create_user(username=f"browser-{name}", tyler_jurisdiction=jurisdiction)
        draft = FilingDraft.objects.create(
            user=user,
            jurisdiction=jurisdiction,
            existing_case="existing",
            workflow_version=2,
            extracted_guesses={"document title": "Motion", "filing type": filing_guess, "docket number": number},
        )
        reviewed_document(draft=draft, role="lead", name="motion.pdf", s3_key=f"{name}.pdf")
        client = Client()
        authorize(client, draft)
        scenarios.append(
            {
                "name": name,
                "jurisdiction": jurisdiction,
                "number": number,
                "rejectGuess": reject_guess,
                "court": "vt:chittenden" if jurisdiction == "vermont" else "336",
                "courtName": "Chittenden Superior Court" if jurisdiction == "vermont" else "Ayer District Court",
                "cookie": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": reverse("extraction_review", kwargs={"jurisdiction": jurisdiction}) + f"?draft={draft.pk}",
            }
        )
    config = tmp_path / "matrix.json"
    config.write_text(
        json.dumps(
            {
                "baseUrl": live_server.url,
                "scenarios": scenarios,
                "evidence": os.getenv("CORE_FILING_UX_EVIDENCE_DIR", str(tmp_path / "evidence")),
            }
        )
    )

    def load_case(draft, _token):
        draft.existing_case_snapshot = loaded_case_snapshot(draft, confirmed=False)
        draft.save()

    choices = [{"value": "motion", "text": "Motion"}, {"value": "answer", "text": "Answer"}]
    with (
        patch("efile.views.extraction_review.cached_account_profile", return_value={}),
        patch("efile.services.people.get_party_types", return_value=[]),
        patch(
            "efile.services.court_selection.fetch_courts",
            return_value=[{"value": "336", "text": "Ayer District Court"}],
        ),
        patch(
            "efile.views.case_lookup.fetch_courts",
            return_value=[{"value": "336", "text": "Ayer District Court"}],
        ),
        patch("efile.views.case_confirmation.load_case", side_effect=load_case),
        patch("efile.views.case_filing_types.permitted_filing_types", return_value=choices),
        patch("efile.views.organize_documents.permitted_filing_types", return_value=choices),
    ):
        result = subprocess.run(
            ["node", "tests/core-filing-ux-browser.js", str(config)],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            timeout=240,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    for draft in FilingDraft.objects.filter(user__username__startswith="browser-"):
        assert draft.existing_case_snapshot["confirmed"]
        assert draft.documents.get().filing_type_code == "motion"
        assert not draft.parties.filter(role="other").exists()
        assert draft.parties.filter(role="filer").count() <= 1


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(not os.getenv("CORE_FILING_UX_BROWSER_TESTS"), reason="Opt-in: requires Chromium")
def test_massachusetts_court_questions_browser(live_server, django_user_model, tmp_path):
    """The real guided court questions, shown only when the case number cannot name the court."""
    lookup = {}
    for name, number in [("blank", ""), ("verified", "1448CV001026")]:
        user = django_user_model.objects.create_user(username=f"court-{name}", tyler_jurisdiction="massachusetts")
        draft = FilingDraft.objects.create(
            user=user, jurisdiction="massachusetts", existing_case="existing", workflow_version=2, docket_number=number
        )
        client = Client()
        authorize(client, draft)
        lookup[name] = {
            "name": name,
            "cookie": client.cookies[settings.SESSION_COOKIE_NAME].value,
            "url": reverse("case_lookup", kwargs={"jurisdiction": "massachusetts"}) + f"?draft={draft.pk}",
        }
    evidence = os.getenv("CORE_FILING_UX_EVIDENCE_DIR", str(tmp_path / "evidence"))
    config = tmp_path / "court-lookup.json"
    config.write_text(
        json.dumps({"baseUrl": live_server.url, "scenarios": [], "courtLookup": lookup, "evidence": evidence})
    )
    courts = [
        {"value": "336", "text": "Ayer District Court"},
        {"value": "1126", "text": "Bristol County Superior Court"},
        {"value": "352", "text": "Essex Probate and Family Court"},
    ]
    with (
        patch("efile.services.court_selection.fetch_courts", return_value=courts),
        patch("efile.api.court_selector_views.fetch_courts", return_value=courts),
        patch("efile.views.case_lookup.fetch_courts", return_value=courts),
    ):
        result = subprocess.run(
            ["node", "tests/core-filing-ux-browser.js", str(config)],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
    assert result.returncode == 0, result.stdout + result.stderr
