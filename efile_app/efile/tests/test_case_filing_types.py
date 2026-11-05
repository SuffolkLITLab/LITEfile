from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft
from efile.services.case_filing_types import case_fingerprint, permitted_filing_types
from efile.tests.helpers import loaded_case_snapshot, reviewed_document
from efile.tests.test_document_extractions import authorize
from efile.views.organize_documents import _save_document_details

pytestmark = pytest.mark.django_db


@pytest.fixture
def confirmed_case(client, django_user_model):
    user = django_user_model.objects.create_user(username="filing-finder", tyler_jurisdiction="vermont")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="vermont",
        existing_case="existing",
        court_code="court",
        previous_case_id="case",
        docket_number="number",
        case_category_code="civil",
        case_type_code="contract",
    )
    draft.existing_case_snapshot = loaded_case_snapshot(draft)
    draft.save()
    reviewed_document(draft=draft, role="lead", name="motion.pdf")
    authorize(client, draft)
    return draft


def test_only_subsequent_types_for_the_confirmed_case_are_requested(confirmed_case):
    response = MagicMock()
    response.json.return_value = [{"code": "motion", "name": "Motion"}]
    with patch("efile.services.case_filing_types.requests.get", return_value=response) as get:
        assert permitted_filing_types(confirmed_case)[0]["value"] == "motion"
    assert get.call_args.kwargs["params"] == {"initial": "false", "category_id": "civil", "type_id": "contract"}


@pytest.mark.parametrize("state", ["unconfirmed", "changed_classification"])
def test_unverified_identity_does_not_query_filing_types(confirmed_case, state):
    if state == "unconfirmed":
        confirmed_case.existing_case_snapshot["confirmed"] = False
    else:
        confirmed_case.case_type_code = "changed"
    with patch("efile.services.case_filing_types.requests.get") as get, pytest.raises(ValueError):
        permitted_filing_types(confirmed_case)
    get.assert_not_called()


@pytest.mark.parametrize("selected,stale", [("initial-complaint", False), ("motion", True), ("", False)])
def test_invalid_or_stale_choices_are_rejected_without_document_changes(confirmed_case, selected, stale):
    doc = confirmed_case.documents.get()
    with (
        patch(
            "efile.views.organize_documents.permitted_filing_types",
            return_value=[{"value": "motion", "text": "Motion"}],
        ),
        pytest.raises(ValueError, match=r"Choose a type of document \(filing code\) for" if not selected else None),
    ):
        _save_document_details(
            confirmed_case,
            [{"id": doc.pk, "filing_type": selected, "document_type": "public"}],
            doc.pk,
            confirmed_case_fingerprint="stale" if stale else case_fingerprint(confirmed_case),
        )
    doc.refresh_from_db()
    assert doc.filing_type_code == ""
    assert doc.role == FilingDocument.Role.LEAD


def test_dropdown_and_finder_selections_use_the_same_validated_save(confirmed_case):
    doc = confirmed_case.documents.get()
    with patch(
        "efile.views.organize_documents.permitted_filing_types",
        return_value=[{"value": "motion", "text": "Court motion"}],
    ):
        _save_document_details(
            confirmed_case,
            [{"id": doc.pk, "filing_type": "motion", "filing_type_name": "untrusted", "document_type": "public"}],
            doc.pk,
            confirmed_case_fingerprint=case_fingerprint(confirmed_case),
        )
    doc.refresh_from_db()
    assert (doc.filing_type_code, doc.filing_type_name) == ("motion", "Court motion")


def test_endpoint_uses_the_draft_instead_of_query_classification(client, confirmed_case):
    with patch("efile.views.case_filing_types.permitted_filing_types", return_value=[]) as choices:
        response = client.get(
            reverse("case_filing_types", kwargs={"jurisdiction": "vermont"}), {"court": "attacker", "initial": "true"}
        )
    assert response.status_code == 200
    assert choices.call_args.args[0].court_code == "court"


def test_court_is_asked_before_the_draft_is_locked(confirmed_case):
    doc = confirmed_case.documents.get()
    events = []
    lock = FilingDraft.objects.select_for_update

    def choices(_draft):
        events.append("court")
        return [{"value": "motion", "text": "Motion"}]

    def locking(*args, **kwargs):
        events.append("lock")
        return lock(*args, **kwargs)

    with (
        patch("efile.views.organize_documents.permitted_filing_types", side_effect=choices),
        patch.object(FilingDraft.objects, "select_for_update", side_effect=locking),
    ):
        _save_document_details(
            confirmed_case,
            [{"id": doc.pk, "filing_type": "motion", "document_type": "public"}],
            doc.pk,
            confirmed_case_fingerprint=case_fingerprint(confirmed_case),
        )
    assert events[:2] == ["court", "lock"]
