import pytest
from django.urls import reverse

from efile.services.extraction_confirmation import extraction_review_fingerprint
from efile.services.filing_type_proposals import current_proposal, resolve_proposal, set_proposal_rejected
from efile.tests.test_case_filing_types import confirmed_case as _confirmed_case

confirmed_case = _confirmed_case
pytestmark = pytest.mark.django_db
CHOICES = [{"value": "motion", "text": "Motion to dismiss"}, {"value": "answer", "text": "Answer"}]


def guess(draft, name):
    draft.extracted_guesses = {"filing type": name}
    draft.save()


def confirm_url():
    return reverse("extraction_review", kwargs={"jurisdiction": "vermont"})


def test_guess_resolves_against_actual_options_without_applying_a_code(confirmed_case):
    guess(confirmed_case, "Motion to dismiss")
    assert resolve_proposal(confirmed_case, CHOICES) == {
        "status": "matched",
        "name": "Motion to dismiss",
        "value": "motion",
    }
    assert not confirmed_case.documents.get().filing_type_code


def test_correction_saved_before_the_field_became_read_only_still_resolves(confirmed_case):
    guess(confirmed_case, "Complaint")
    confirmed_case.filing_type_proposal = {
        "name": "Motion to dismiss",
        "source": "user",
        "source_fingerprint": extraction_review_fingerprint(confirmed_case),
    }
    confirmed_case.save()
    assert resolve_proposal(confirmed_case, CHOICES)["value"] == "motion"


def test_unavailable_or_ambiguous_suggestion_is_not_selected(confirmed_case):
    guess(confirmed_case, "Complaint")
    assert resolve_proposal(confirmed_case, CHOICES)["status"] == "unavailable"
    guess(confirmed_case, "Answer")
    assert resolve_proposal(confirmed_case, CHOICES + [{"value": "other", "text": "Answer"}])["status"] == "unavailable"


def test_rejected_suggestion_is_not_offered_and_can_be_restored(confirmed_case):
    guess(confirmed_case, "Answer")
    set_proposal_rejected(confirmed_case, True)
    confirmed_case.refresh_from_db()
    assert current_proposal(confirmed_case)["name"] == "Answer"
    assert resolve_proposal(confirmed_case, CHOICES) == {"status": "none"}
    set_proposal_rejected(confirmed_case, False)
    assert resolve_proposal(confirmed_case, CHOICES)["status"] == "matched"


def test_replacement_invalidates_a_rejection(confirmed_case):
    guess(confirmed_case, "Answer")
    set_proposal_rejected(confirmed_case, True)
    confirmed_case.documents.update(s3_key="replacement.pdf")
    assert not current_proposal(confirmed_case).get("rejected")


def test_opt_out_keeps_the_ordinary_selection_path(confirmed_case):
    guess(confirmed_case, "Answer")
    confirmed_case.ai_assistance_opted_out = True
    assert resolve_proposal(confirmed_case, CHOICES) == {"status": "none"}


def test_confirm_shows_the_suggestion_read_only(client, confirmed_case):
    guess(confirmed_case, "Complaint")
    content = client.get(confirm_url()).content.decode()
    assert "Complaint" in content.partition('id="filing-proposal"')[2]
    assert 'name="proposed_filing_type"' not in content
    assert 'name="reject_filing_proposal"' in content


def test_confirm_ignores_a_typed_filing_code(client, confirmed_case):
    guess(confirmed_case, "Complaint")
    response = client.post(confirm_url(), {"existing_case": "existing", "proposed_filing_type": "Answer"})
    assert response.status_code == 302
    confirmed_case.refresh_from_db()
    assert current_proposal(confirmed_case)["name"] == "Complaint"


def test_confirm_saves_a_rejection_and_shows_it_ticked_on_return(client, confirmed_case):
    guess(confirmed_case, "Complaint")
    response = client.post(confirm_url(), {"existing_case": "existing", "reject_filing_proposal": "yes"})
    assert response.status_code == 302
    confirmed_case.refresh_from_db()
    assert resolve_proposal(confirmed_case, CHOICES) == {"status": "none"}
    assert not confirmed_case.documents.get().filing_type_code
    assert client.get(confirm_url()).context["filing_type_proposal"]["rejected"]

    assert client.post(confirm_url(), {"existing_case": "existing"}).status_code == 302
    confirmed_case.refresh_from_db()
    assert resolve_proposal(confirmed_case, CHOICES)["status"] == "unavailable"


def test_suggestion_cleared_before_the_field_became_read_only_stays_hidden(client, confirmed_case):
    guess(confirmed_case, "Complaint")
    confirmed_case.filing_type_proposal = {
        "name": "",
        "source": "user",
        "source_fingerprint": extraction_review_fingerprint(confirmed_case),
    }
    confirmed_case.save()
    assert 'id="filing-proposal"' not in client.get(confirm_url()).content.decode()
    assert resolve_proposal(confirmed_case, CHOICES) == {"status": "none"}


def test_rejection_survives_a_refused_switch_to_a_new_case(client, confirmed_case):
    guess(confirmed_case, "Complaint")
    set_proposal_rejected(confirmed_case, True)
    response = client.post(confirm_url(), {"existing_case": "new"})  # no court or case type
    assert response.status_code == 200
    assert response.context["filing_type_proposal"]["rejected"]
    confirmed_case.refresh_from_db()
    assert current_proposal(confirmed_case)["rejected"]


def test_refused_document_approval_keeps_the_unsaved_rejection(client, confirmed_case):
    guess(confirmed_case, "Complaint")
    response = client.post(
        confirm_url(),
        {"existing_case": "existing", "reject_filing_proposal": "yes", "preview_fingerprint": "stale"},
    )
    assert response.status_code != 302
    assert response.context["filing_type_proposal"]["rejected"]
    confirmed_case.refresh_from_db()
    assert not current_proposal(confirmed_case).get("rejected")
