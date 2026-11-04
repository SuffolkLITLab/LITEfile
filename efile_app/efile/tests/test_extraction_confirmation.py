import pytest
from django.urls import reverse

from efile.models import FilingDocument
from efile.services.extracted_parties import review_rows
from efile.services.extraction_confirmation import extraction_is_confirmed, extraction_review_fingerprint
from efile.tests.test_extracted_parties import authorize
from efile.tests.test_extracted_parties import review_draft as _review_draft

review_draft = _review_draft

pytestmark = pytest.mark.django_db


def test_confirmation_survives_case_edits_but_not_replacement(review_draft):
    draft = review_draft
    draft.extraction_review_fingerprint = extraction_review_fingerprint(draft)
    draft.save()
    draft.court_code = "changed-court"
    assert extraction_is_confirmed(draft)
    FilingDocument.objects.filter(draft=draft).update(s3_key="replacement.pdf")
    assert not extraction_is_confirmed(draft)


def test_deleted_extracted_names_do_not_reappear(review_draft):
    assert review_rows(review_draft)
    review_draft.extraction_review_fingerprint = extraction_review_fingerprint(review_draft)
    review_draft.save()
    assert review_rows(review_draft) == []


def test_no_names_means_no_party_editor(client, review_draft):
    authorize(client, review_draft)
    review_draft.extracted_guesses = {"document title": "Motion"}
    review_draft.save()
    response = client.get(reverse("extraction_review", kwargs={"jurisdiction": "illinois"}))
    assert b'id="review-parties"' not in response.content
