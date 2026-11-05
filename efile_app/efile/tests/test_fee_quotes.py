"""A fee quote is only current while the draft still matches what it priced (#193)."""

import json
import re
from unittest.mock import patch

import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft, FilingParty
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.fee_quotes import (
    FeeQuoteState,
    fee_fingerprint,
    fee_quote_state,
    invalidate_fee_quote,
    quote_from_efsp_response,
    record_fee_quote,
)
from efile.tests.helpers import accepted_submission, reviewed_document
from efile.workflow import WorkflowStepKey

REVIEW_URL = reverse("case_review", kwargs={"jurisdiction": "illinois"})
SUBMIT_URL = reverse("submit_final_filing")
FEES_URL = reverse("api:payment_fees")


def efsp_fee_response(total, *fees):
    return {
        "feesCalculationAmount": {"value": total},
        "allowanceCharge": [
            {
                "chargeIndicator": {"value": True},
                "allowanceChargeReason": {"value": label},
                "amount": {"value": amount},
            }
            for label, amount in fees
        ],
    }


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._payload


@pytest.fixture
def draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="fee-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        current_step=WorkflowStepKey.REVIEW,
        existing_case="new",
        court_code="cook:law1",
        court_name="Circuit Court of Cook County",
        case_category_code="civil",
        case_category_name="Civil",
        case_type_code="contract",
        case_type_name="Contract",
        document_checklist_acknowledged=True,
        selected_payment_account_id="pay-123",
        selected_payment_account_name="Card ending in 4242",
        selected_payment_account_type="CC",
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="Complaint.pdf",
        filing_type_code="complaint",
        filing_type_name="Complaint",
        document_type_code="public",
        document_type_name="Public",
        requested_optional_services=["certified-copy"],
    )
    FilingParty.objects.create(
        draft=draft,
        role="filer",
        sort_order=0,
        party_type="PLA",
        party_type_name="Plaintiff",
        first_name="Jordan",
        last_name="Taylor",
        email="jordan@example.com",
        address_line_1="123 Main Street",
        city="Springfield",
        state="IL",
        zip_code="62701",
        is_filing_party=True,
    )
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "test-token"}
    session.save()
    return draft


def page_token(client):
    """The fee-inputs token Review hands its script, as a filer's browser has it."""

    content = client.get(REVIEW_URL).content.decode()
    token = re.search(r'<script id="fee-inputs-token" type="application/json">(.*?)</script>', content)
    assert token is not None, "Review carries no fee-inputs token"
    return json.loads(token.group(1))


def quote_fees(client, total="118.00", *fees, token=None, during=None):
    """Ask the fee API, as Payment and Review do, with the EFSP answering `total`.

    `token` defaults to the one the Review page carries now; `during` runs while
    the EFSP is being asked, as an edit in another tab would.
    """

    token = page_token(client) if token is None else token
    answer = FakeResponse(200, efsp_fee_response(total, *fees))

    def efsp(*args, **kwargs):
        if during:
            during()
        return answer

    with (
        patch("efile.api.filing_views.prepare_efile_payload"),
        patch("efile.api.filing_views.requests.post", side_effect=efsp),
    ):
        return client.post(
            FEES_URL,
            data=json.dumps(
                {"efile_data": {"al_court_bundle": []}, "payment_account_id": "pay-123", "fee_inputs_token": token}
            ),
            content_type="application/json",
        )


def submit(client):
    draft = FilingDraft.objects.get(pk=client.session[CURRENT_DRAFT_SESSION_KEY])
    with (
        patch("efile.views.session_api.prepare_efile_payload", return_value={"al_court_bundle": {}}),
        patch("requests.post", return_value=FakeResponse(200, {"filing_id": "ENV-1"})) as post,
    ):
        response = client.post(
            SUBMIT_URL,
            data=json.dumps(accepted_submission(draft, efile_data={"al_court_bundle": {}})),
            content_type="application/json",
        )
    return response, post


# --- What counts as fee-affecting ---------------------------------------------


def _set(draft, **values):
    FilingDraft.objects.filter(pk=draft.pk).update(**values)
    draft.refresh_from_db()


def _lead(draft):
    return draft.documents.get(role="lead")


FEE_AFFECTING_EDITS = {
    "court": lambda draft: _set(draft, court_code="cook:cvd1"),
    "case category": lambda draft: _set(draft, case_category_code="family"),
    "case type": lambda draft: _set(draft, case_type_code="tort"),
    "new or existing case": lambda draft: _set(draft, existing_case="existing"),
    "existing case identity": lambda draft: _set(draft, previous_case_id="case-9"),
    "amount in controversy": lambda draft: _set(draft, amount_in_controversy="12000"),
    "payment account": lambda draft: _set(draft, selected_payment_account_id="pay-456"),
    "filing type": lambda draft: FilingDocument.objects.filter(pk=_lead(draft).pk).update(filing_type_code="answer"),
    "filing component": lambda draft: FilingDocument.objects.filter(pk=_lead(draft).pk).update(
        filing_component_code="attachment"
    ),
    "optional service removed": lambda draft: FilingDocument.objects.filter(pk=_lead(draft).pk).update(
        requested_optional_services=[]
    ),
    "optional service added": lambda draft: FilingDocument.objects.filter(pk=_lead(draft).pk).update(
        requested_optional_services=["certified-copy", "service-by-mail"]
    ),
    "document added": lambda draft: reviewed_document(
        draft=draft, role=FilingDocument.Role.SUPPORTING, sort_order=0, filing_type_code="exhibit"
    ),
    "party added": lambda draft: FilingParty.objects.create(
        draft=draft, role="other", sort_order=1, party_type="DEF", first_name="Acme"
    ),
}


@pytest.mark.django_db
@pytest.mark.parametrize("edit", FEE_AFFECTING_EDITS.values(), ids=FEE_AFFECTING_EDITS.keys())
def test_every_fee_affecting_edit_makes_the_quote_stale(draft, edit):
    record_fee_quote(draft, "118.00", [])
    assert fee_quote_state(draft) == FeeQuoteState.CURRENT

    edit(draft)

    draft.refresh_from_db()
    assert fee_quote_state(draft) == FeeQuoteState.STALE


@pytest.mark.django_db
def test_edits_that_do_not_price_the_filing_leave_the_quote_current(draft):
    record_fee_quote(draft, "118.00", [])

    _set(draft, case_title="Taylor v. Acme", notice_email="notices@example.com", name_change_reason="Marriage")
    FilingParty.objects.filter(draft=draft).update(address_line_1="9 Elm Street", phone="312-555-0100")
    FilingDocument.objects.filter(draft=draft).update(name="Renamed.pdf", courtesy_copy_email="copy@example.com")

    draft.refresh_from_db()
    assert fee_quote_state(draft) == FeeQuoteState.CURRENT


@pytest.mark.django_db
def test_a_quote_saved_before_fingerprints_is_treated_as_stale(draft):
    _set(draft, quoted_fee_total="118.00", quoted_fee_breakdown=[], quoted_fee_fingerprint="")

    assert fee_quote_state(draft) == FeeQuoteState.STALE


@pytest.mark.django_db
def test_a_waiver_needs_no_quote_and_no_quote_is_missing(draft):
    assert fee_quote_state(draft) == FeeQuoteState.MISSING
    _set(draft, selected_payment_account_type="WV")
    assert fee_quote_state(draft) == FeeQuoteState.WAIVED


@pytest.mark.django_db
def test_invalidate_forgets_the_quote(draft):
    record_fee_quote(draft, "118.00", [{"label": "Filing fee", "amount": "118.00"}])
    invalidate_fee_quote(draft)
    draft.refresh_from_db()
    assert (draft.quoted_fee_total, draft.quoted_fee_breakdown, draft.quoted_fee_fingerprint) == ("", [], "")


@pytest.mark.django_db
def test_the_quote_is_priced_for_the_account_it_was_asked_for(draft):
    """Payment asks with an account the filer has not saved yet."""

    record_fee_quote(draft, "118.00", [], payment_account_id="pay-456")
    assert fee_quote_state(draft) == FeeQuoteState.STALE
    _set(draft, selected_payment_account_id="pay-456")
    assert draft.quoted_fee_fingerprint == fee_fingerprint(draft)
    assert fee_quote_state(draft) == FeeQuoteState.CURRENT


def test_reading_the_efsp_fee_response():
    assert quote_from_efsp_response(efsp_fee_response("118", ("Filing fee", "100.00"), ("E-filing fee", "18.00"))) == (
        "118.00",
        [{"label": "Filing fee", "amount": "100.00"}, {"label": "E-filing fee", "amount": "18.00"}],
    )
    assert quote_from_efsp_response({"allowanceCharge": []}) is None
    assert quote_from_efsp_response({"feesCalculationAmount": {"value": "lots"}}) is None


# --- The fee API records the quote --------------------------------------------


@pytest.mark.django_db
def test_the_fee_api_records_the_quote_with_what_it_priced(client, draft):
    response = quote_fees(client, "118.00", ("Filing fee", "118.00"))

    body = response.json()
    assert body["quote_recorded"] is True
    assert body["quote"] == {
        "state": "current",
        "is_zero": False,
        "total": "118.00",
        "breakdown": [{"label": "Filing fee", "amount": "118.00"}],
    }
    draft.refresh_from_db()
    assert draft.quoted_fee_total == "118.00"
    assert fee_quote_state(draft) == FeeQuoteState.CURRENT


@pytest.mark.django_db
def test_an_edit_while_the_efsp_is_pricing_keeps_the_old_answer_out(client, draft):
    """The reviewer's case: another tab changes the filing mid-request."""

    response = quote_fees(client, "118.00", during=lambda: _set(draft, case_type_code="tort"))

    body = response.json()
    assert body["quote_recorded"] is False
    assert body["quote_superseded"] is True
    draft.refresh_from_db()
    assert draft.quoted_fee_total == ""
    assert fee_quote_state(draft) == FeeQuoteState.MISSING


@pytest.mark.django_db
def test_a_request_built_from_an_older_page_is_not_recorded(client, draft):
    token = page_token(client)
    FilingDocument.objects.filter(draft=draft).update(requested_optional_services=[])

    body = quote_fees(client, "128.00", token=token).json()

    assert (body["quote_recorded"], body["quote_superseded"]) == (False, True)
    draft.refresh_from_db()
    assert fee_quote_state(draft) == FeeQuoteState.MISSING


@pytest.mark.django_db
def test_a_request_that_does_not_say_what_it_priced_is_not_recorded(client, draft):
    body = quote_fees(client, "118.00", token="").json()

    assert body["quote_recorded"] is False
    draft.refresh_from_db()
    assert fee_quote_state(draft) == FeeQuoteState.MISSING


@pytest.mark.django_db
def test_an_edit_after_recording_still_makes_the_quote_stale(client, draft):
    quote_fees(client, "118.00")
    _set(draft, court_code="cook:cvd1")

    assert fee_quote_state(draft) == FeeQuoteState.STALE


@pytest.mark.django_db
def test_a_fee_answer_without_a_total_is_not_recorded(client, draft):
    with (
        patch("efile.api.filing_views.prepare_efile_payload"),
        patch("efile.api.filing_views.requests.post", return_value=FakeResponse(200, {"allowanceCharge": []})),
    ):
        response = client.post(
            FEES_URL,
            data=json.dumps(
                {
                    "efile_data": {"al_court_bundle": []},
                    "payment_account_id": "pay-123",
                    "fee_inputs_token": page_token(client),
                }
            ),
            content_type="application/json",
        )

    assert response.json()["quote_recorded"] is False
    draft.refresh_from_db()
    assert fee_quote_state(draft) == FeeQuoteState.MISSING


# --- Review and submission ----------------------------------------------------


@pytest.mark.django_db
def test_review_does_not_show_a_stale_total_as_current(client, draft):
    record_fee_quote(draft, "118.00", [{"label": "Filing fee", "amount": "118.00"}])
    FilingDocument.objects.filter(draft=draft).update(requested_optional_services=[])

    content = client.get(REVIEW_URL).content.decode()

    assert 'data-state="stale"' in content
    assert "$118.00" not in content
    assert "changed since they were calculated" in content
    assert '<script id="fee-quote-state" type="application/json">"stale"</script>' in content


@pytest.mark.django_db
def test_review_shows_a_current_total(client, draft):
    record_fee_quote(draft, "118.00", [{"label": "Filing fee", "amount": "118.00"}])

    content = client.get(REVIEW_URL).content.decode()

    assert 'data-state="current"' in content
    assert "$118.00" in content


@pytest.mark.django_db
@pytest.mark.parametrize("quoted", [False, True], ids=["no quote", "stale quote"])
def test_submission_is_refused_without_a_current_quote(client, draft, quoted):
    if quoted:
        record_fee_quote(draft, "118.00", [])
        _set(draft, case_type_code="tort")

    response, post = submit(client)

    assert response.status_code == 412
    assert response.json()["error_code"] == "submission_fee_quote_stale"
    post.assert_not_called()
    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.DRAFT


@pytest.mark.django_db
def test_a_fee_waiver_submits_without_a_quote(client, draft):
    _set(draft, selected_payment_account_type="WV")

    response, post = submit(client)

    assert response.status_code == 200, response.content
    post.assert_called_once()


@pytest.mark.django_db
def test_removing_an_optional_service_is_requoted_from_review_without_visiting_payment(client, draft, monkeypatch):
    monkeypatch.setattr(
        "efile.services.document_choices.filing_choices", lambda draft: [{"value": "complaint", "text": "Complaint"}]
    )
    """The optional-services regression: no trip back to Payment is needed."""

    quote_fees(client, "128.00", ("Filing fee", "118.00"), ("Certified copy", "10.00"))
    lead = _lead(draft)

    # The filer goes back to Organize documents from Review and drops the copy.
    organized = client.post(
        reverse("organize_documents", kwargs={"jurisdiction": "illinois"}) + "?return_to=review",
        {
            "documents": [
                {
                    "id": lead.pk,
                    "filing_type": "complaint",
                    "filing_type_name": "Complaint",
                    "document_type": "public",
                    "requested_optional_services": [],
                }
            ],
            "main_document_id": lead.pk,
        },
        content_type="application/json",
    )
    assert organized.status_code == 200

    # Review no longer offers the old total, and will not submit against it.
    review = client.get(REVIEW_URL).content.decode()
    assert 'data-state="stale"' in review
    assert "$128.00" not in review
    assert submit(client)[0].status_code == 412

    # What review.js does on that page: ask for fees again, from Review.
    requoted = quote_fees(client, "118.00", ("Filing fee", "118.00")).json()
    assert requoted["quote"]["state"] == "current"
    assert requoted["quote"]["total"] == "118.00"

    response, post = submit(client)
    assert response.status_code == 200
    post.assert_called_once()
