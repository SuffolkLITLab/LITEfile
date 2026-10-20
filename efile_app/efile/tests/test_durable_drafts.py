import json

import pytest
from django.urls import reverse
from django.utils import timezone

from efile.models import FilingDocument, FilingDraft, FilingParty, FilingPlan
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY, get_current_draft
from efile.services.drafts import (
    draft_snapshot,
    read_case_data,
    read_upload_data,
    write_case_data,
    write_upload_data,
)
from efile.services.efsp_payload import PayloadValidationError
from efile.services.fee_quotes import record_fee_quote
from efile.tests.helpers import accepted_submission, reviewed_document
from efile.workflow import WorkflowStepKey, get_workflow_step_choices


class FakeApiResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload


def _authorize_jurisdiction_session(client, jurisdiction="illinois"):
    """Give the session the Tyler token that draft creation/updates now require."""
    session = client.session
    session["auth_tokens"] = {f"TYLER-TOKEN-{jurisdiction.upper()}": "token"}
    session.save()


def _prepare_submission(client, draft, monkeypatch, jurisdiction="illinois"):
    """Populate the draft (the source of truth) and session so submit can run."""
    write_case_data(draft, {"court": "cook:cd"})
    write_upload_data(
        draft,
        {
            "files": {"lead": {"url": "https://example.com/petition.pdf", "s3_key": "owned/petition.pdf"}},
            "lead_filing_type": "petition",
            "lead_filing_component": "lead",
        },
    )
    monkeypatch.setattr(
        "efile.services.efsp_payload.S3UploadHandler.get_public_url",
        lambda *args, **kwargs: "https://storage.invalid/petition.pdf",
    )
    monkeypatch.setattr("efile.services.efsp_payload._EfspLookups.get", lambda *args: [])
    draft.documents.update(preparation="unchanged", preparation_reviewed_at=timezone.now())
    # Submission is only offered against a quote that still prices the filing.
    record_fee_quote(draft, "0.00", [])
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = jurisdiction
    session["auth_tokens"] = {f"TYLER-TOKEN-{jurisdiction.upper()}": "token"}
    session.save()


@pytest.mark.django_db
def test_write_case_data_normalizes_known_fields(django_user_model):
    user = django_user_model.objects.create_user(username="draft-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    case_data = {
        "court": "cook:cd",
        "court_name": "Cook County Circuit Court",
        "case_category": "MR",
        "case_type": "Name Change",
        "filing_type": "motion",
        "document_type": "petition",
        "selected_payment_account": "pay-123",
        "selected_payment_account_name": "Card ending in 4242",
        "optional_services": ["certified_copy"],
        "petitioner_first_name": "Ada",
        "petitioner_last_name": "Lovelace",
        "petitioner_email": "ada@example.com",
        "petitioner_party_type": "PET",
        "new_first_name": "Augusta Ada",
        "new_last_name": "Lovelace",
    }

    write_case_data(draft, case_data, current_step=WorkflowStepKey.CASE_INFORMATION)
    draft.refresh_from_db()

    assert draft.current_step == WorkflowStepKey.CASE_INFORMATION
    assert draft.court_code == "cook:cd"
    assert draft.case_category_code == "MR"
    assert draft.case_type_code == "Name Change"
    assert draft.filing_type_code == "motion"
    assert draft.document_type_code == "petition"
    assert draft.selected_payment_account_id == "pay-123"
    assert draft.optional_services == ["certified_copy"]

    petitioner = FilingParty.objects.get(draft=draft, role="petitioner")
    assert petitioner.first_name == "Ada"
    assert petitioner.last_name == "Lovelace"
    assert petitioner.email == "ada@example.com"
    assert petitioner.party_type == "PET"

    new_name = FilingParty.objects.get(draft=draft, role="new_name")
    assert new_name.first_name == "Augusta Ada"


@pytest.mark.django_db
def test_write_case_data_does_not_persist_unknown_keys(django_user_model):
    """Only modelled fields survive; there is no catch-all blob for random keys."""
    user = django_user_model.objects.create_user(username="typed-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")

    write_case_data(draft, {"court": "cook:cd", "totally_made_up_field": "should be dropped"})
    draft.refresh_from_db()

    assert draft.court_code == "cook:cd"
    assert "totally_made_up_field" not in read_case_data(draft)


@pytest.mark.django_db
def test_case_data_round_trips_through_the_model(django_user_model):
    user = django_user_model.objects.create_user(username="round-trip-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    case_data = {
        "court": "cook:cd",
        "case_category": "MR",
        "case_type": "Name Change",
        "filing_type": "motion",
        "document_type": "petition",
        "existing_case": "no",
        "petitioner_first_name": "Ada",
        "petitioner_party_type": "PET",
        "other_first_name": "Grace",
        "other_address_city": "Chicago",
    }

    write_case_data(draft, case_data)
    blob = read_case_data(draft)

    assert blob["court"] == "cook:cd"
    assert blob["case_type"] == "Name Change"
    assert blob["petitioner_first_name"] == "Ada"
    # petitioner party type is echoed under all three legacy aliases the browser reads
    assert blob["party_type"] == "PET"
    assert blob["determined_party_type"] == "PET"
    assert blob["other_first_name"] == "Grace"
    assert blob["other_address_city"] == "Chicago"


@pytest.mark.django_db
def test_defendant_fields_round_trip_as_respondent_aliases(django_user_model):
    user = django_user_model.objects.create_user(username="divorce-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")

    write_case_data(
        draft,
        {
            "defendant_party_type": "DEF",
            "defendant_first_name": "Grace",
            "defendant_last_name": "Hopper",
        },
    )

    respondent = FilingParty.objects.get(draft=draft, role="respondent")
    assert respondent.party_type == "DEF"
    assert respondent.first_name == "Grace"
    assert respondent.last_name == "Hopper"

    blob = read_case_data(draft)
    assert blob["respondent_name_party_type"] == "DEF"
    assert blob["defendant_party_type"] == "DEF"
    assert blob["respondent_first_name"] == "Grace"
    assert blob["defendant_first_name"] == "Grace"


@pytest.mark.django_db
def test_amount_in_controversy_is_read_back_from_the_draft(django_user_model):
    """case_questions saves this directly on the model (it's not config-driven,
    so it doesn't go through write_case_data), but the frontend still reads it
    out of the same case_data blob everything else does."""
    user = django_user_model.objects.create_user(username="amount-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", amount_in_controversy="12500.00")

    assert read_case_data(draft)["amount_in_controversy"] == "12500.00"


@pytest.mark.django_db
def test_supplemental_case_fields_round_trip(django_user_model):
    """Config-driven questionnaire answers survive a durable-draft round trip."""
    user = django_user_model.objects.create_user(username="supplemental-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")

    write_case_data(draft, {"has_children": "false", "child_count": "2", "unknown_answer": "drop me"})
    draft.refresh_from_db()

    assert draft.supplemental_fields == {"has_children": "false", "child_count": "2"}
    assert read_case_data(draft)["has_children"] == "false"
    assert read_case_data(draft)["child_count"] == "2"
    assert (snapshot := draft_snapshot(draft)) is not None
    assert snapshot["supplemental_fields"] == {"has_children": "false", "child_count": "2"}


@pytest.mark.django_db
def test_existing_case_lookup_codes_are_persisted(django_user_model):
    """The existing-case lookup sends *_code keys; they must not be dropped."""
    user = django_user_model.objects.create_user(username="existing-case-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")

    write_case_data(
        draft,
        {
            "existing_case": "yes",
            "court": "cook:cd",
            "case_category_code": "MR",
            "case_category_name": "Miscellaneous Remedy",
            "case_type_code": "Name Change",
            "case_type_name": "Change of Name",
            "case_tracking_id": "track-1",
            "case_docket_id": "2024-MR-1",
        },
    )
    draft.refresh_from_db()

    assert draft.case_category_code == "MR"
    assert draft.case_type_code == "Name Change"
    assert draft.previous_case_id == "track-1"
    assert draft.docket_number == "2024-MR-1"


@pytest.mark.django_db
def test_write_upload_data_creates_lead_and_supporting_documents(django_user_model):
    user = django_user_model.objects.create_user(username="document-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    upload_data = {
        "files": {
            "lead": {
                "name": "petition.pdf",
                "url": "https://example.com/petition.pdf",
                "s3_key": "drafts/petition.pdf",
                "size": 1234,
                "type": "application/pdf",
            },
            "supporting": [
                {
                    "name": "order.pdf",
                    "url": "https://example.com/order.pdf",
                    "size": 4321,
                    "type": "application/pdf",
                }
            ],
        },
        "guesses": {"court": "Cook County"},
        "lead_filing_type": "efile",
        "lead_document_type": "petition",
        "lead_filing_component": "lead",
        "supporting_documents": [
            {
                "filing_type": "attachment",
                "document_type": "exhibit",
                "filing_component": "supporting",
                "cc_email": "copy@example.com",
            }
        ],
    }

    write_upload_data(draft, upload_data, current_step=WorkflowStepKey.DOCUMENTS)
    draft.refresh_from_db()

    assert draft.current_step == WorkflowStepKey.DOCUMENTS
    assert draft.extracted_guesses == {"court": "Cook County"}

    lead = FilingDocument.objects.get(draft=draft, role=FilingDocument.Role.LEAD)
    assert lead.name == "petition.pdf"
    assert lead.s3_key == "drafts/petition.pdf"
    assert lead.content_type == "application/pdf"
    assert lead.filing_type_code == "efile"

    supporting = FilingDocument.objects.get(draft=draft, role=FilingDocument.Role.SUPPORTING)
    assert supporting.name == "order.pdf"
    assert supporting.document_type_code == "exhibit"
    assert supporting.courtesy_copy_email == "copy@example.com"


@pytest.mark.django_db
def test_upload_data_round_trips_through_the_model(django_user_model):
    user = django_user_model.objects.create_user(username="upload-round-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    write_upload_data(
        draft,
        {
            "files": {"lead": {"name": "petition.pdf", "url": "https://example.com/petition.pdf"}},
            "lead_filing_type": "efile",
            "guesses": {"court": "Cook County"},
        },
    )

    blob = read_upload_data(draft)

    assert blob["files"]["lead"]["name"] == "petition.pdf"
    assert blob["files"]["lead"]["url"] == "https://example.com/petition.pdf"
    assert blob["lead_filing_type"] == "efile"
    assert blob["guesses"] == {"court": "Cook County"}


@pytest.mark.django_db
def test_clearing_the_lead_services_does_not_fall_back_to_the_case_level_list(django_user_model):
    """An empty list is the filer's answer, not the absence of one.

    ``optional_services`` is also the key the *case* keeps its own list under,
    so falling through on an empty lead list refills the lead document from an
    unrelated list -- and bills the filer for services they just deselected.
    """

    user = django_user_model.objects.create_user(username="lead-services-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    write_upload_data(draft, {"files": {"lead": {"name": "petition.pdf"}}, "lead_optional_services": ["143487"]})
    assert FilingDocument.objects.get(draft=draft, role=FilingDocument.Role.LEAD).requested_optional_services == [
        "143487"
    ]

    write_upload_data(
        draft,
        {
            "files": {"lead": {"name": "petition.pdf"}},
            "lead_optional_services": [],
            "optional_services": ["999999"],
        },
    )

    lead = FilingDocument.objects.get(draft=draft, role=FilingDocument.Role.LEAD)
    assert lead.requested_optional_services == []


@pytest.mark.django_db
def test_an_older_blob_still_reads_the_bare_optional_services_key(django_user_model):
    """The fallback stays for clients that never sent the lead-specific key."""
    user = django_user_model.objects.create_user(username="legacy-services-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")

    write_upload_data(draft, {"files": {"lead": {"name": "petition.pdf"}}, "optional_services": ["143487"]})

    lead = FilingDocument.objects.get(draft=draft, role=FilingDocument.Role.LEAD)
    assert lead.requested_optional_services == ["143487"]


@pytest.mark.django_db
def test_supporting_documents_are_replaced_wholesale(django_user_model):
    user = django_user_model.objects.create_user(username="supporting-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    write_upload_data(draft, {"files": {"supporting": [{"name": "first.pdf"}, {"name": "second.pdf"}]}})
    assert draft.documents.filter(role=FilingDocument.Role.SUPPORTING).count() == 2

    write_upload_data(draft, {"files": {"supporting": [{"name": "only.pdf"}]}})

    supporting = draft.documents.filter(role=FilingDocument.Role.SUPPORTING)
    assert supporting.count() == 1
    assert supporting.first().name == "only.pdf"


@pytest.mark.django_db
def test_draft_snapshot_is_json_serializable(django_user_model):
    user = django_user_model.objects.create_user(username="snapshot-owner", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", court_code="cook:cd")

    snapshot = draft_snapshot(draft)

    assert snapshot is not None
    assert snapshot["id"] == draft.pk
    assert snapshot["court_code"] == "cook:cd"
    json.dumps(snapshot)


@pytest.mark.django_db
def test_options_page_points_resume_to_draft_workflow_step(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="resume-user",
        password="testpass123",
        tyler_jurisdiction="illinois",
    )
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        current_step=WorkflowStepKey.DOCUMENTS,
    )
    client.force_login(user)
    _authorize_jurisdiction_session(client)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session.save()

    response = client.get(reverse("efile_options", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert reverse("organize_documents", kwargs={"jurisdiction": "illinois"}).encode() in response.content


@pytest.mark.django_db
def test_legacy_documents_url_redirects_into_reorganized_document_flow(client, django_user_model):
    user = django_user_model.objects.create_user(username="missing-lead-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    write_case_data(draft, {"court": "cook:cd", "case_type": "Name Change"})
    client.force_login(user)
    _authorize_jurisdiction_session(client)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session.save()

    response = client.get(reverse("upload", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 302
    assert response.url == reverse("organize_documents", kwargs={"jurisdiction": "illinois"}) + f"?draft={draft.pk}"


@pytest.mark.django_db
def test_current_draft_enforces_owner(client, django_user_model):
    illinois_user = django_user_model.objects.create_user(username="illinois-user", tyler_jurisdiction="illinois")
    other_user = django_user_model.objects.create_user(username="other-user", tyler_jurisdiction="massachusetts")
    other_draft = FilingDraft.objects.create(user=other_user, jurisdiction="illinois")
    FilingDraft.objects.create(user=illinois_user, jurisdiction="illinois")

    client.force_login(illinois_user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = other_draft.pk
    session.save()

    response = client.get(reverse("get_current_draft"))

    assert response.status_code == 200
    assert response.json()["data"]["filing_draft"] is None


@pytest.mark.django_db
def test_current_draft_does_not_cross_jurisdictions(client, django_user_model):
    user = django_user_model.objects.create_user(username="multi-state-user", tyler_jurisdiction="illinois")
    FilingDraft.objects.create(user=user, jurisdiction="illinois")
    massachusetts_draft = FilingDraft.objects.create(user=user, jurisdiction="massachusetts")
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = massachusetts_draft.pk
    session.save()

    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    current = get_current_draft(request, jurisdiction="illinois")

    assert current is None


@pytest.mark.django_db
def test_save_case_endpoint_persists_into_current_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="endpoint-user", tyler_jurisdiction="illinois")
    client.force_login(user)
    _authorize_jurisdiction_session(client)
    client.post(reverse("start_filing", kwargs={"jurisdiction": "illinois"}), {"existing_case": "new"})

    response = client.post(
        reverse("save_case_data_api"),
        data={
            "jurisdiction": "illinois",
            "data": {
                "existing_case": "no",
                "court": "cook:cd",
                "case_type": "Name Change",
                "petitioner_first_name": "Ada",
                "petitioner_last_name": "Lovelace",
            },
        },
        content_type="application/json",
    )

    assert response.status_code == 200
    draft = FilingDraft.objects.get(user=user)
    assert draft.court_code == "cook:cd"
    assert draft.case_type_code == "Name Change"
    assert draft.parties.get(role="petitioner").first_name == "Ada"


@pytest.mark.django_db
def test_save_case_endpoint_requires_authentication(client):
    response = client.post(
        reverse("save_case_data_api"),
        data={"jurisdiction": "illinois", "data": {"court": "cook:cd"}},
        content_type="application/json",
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_final_submission_marks_current_draft_submitted(client, django_user_model, monkeypatch):
    request_options = {}

    def fake_post(*_args, **_kwargs):
        request_options.update(_kwargs)
        return FakeApiResponse(201, {"filing_id": "abc-123"})

    monkeypatch.setattr("requests.post", fake_post)
    user = django_user_model.objects.create_user(username="submit-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", current_step=WorkflowStepKey.REVIEW)
    client.force_login(user)
    _prepare_submission(client, draft, monkeypatch)

    response = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.SUBMITTED
    assert draft.current_step == WorkflowStepKey.CONFIRMATION
    assert draft.submission_response == {"filing_id": "abc-123"}
    assert CURRENT_DRAFT_SESSION_KEY not in client.session
    assert request_options["timeout"] == (10, 300)


@pytest.mark.django_db
def test_final_submission_marks_attached_plan_documents_as_filed(client, django_user_model, monkeypatch):
    def fake_post(*_args, **_kwargs):
        return FakeApiResponse(201, {"filing_id": "abc-123"})

    monkeypatch.setattr("requests.post", fake_post)
    user = django_user_model.objects.create_user(username="plan-submit-user", tyler_jurisdiction="illinois")
    plan = FilingPlan.objects.create(
        user=user,
        title="Name change",
        jurisdiction="illinois",
        checklist={"petition": {"label": "Petition", "requirement": "always", "status": "have"}},
    )
    draft = FilingDraft.objects.create(
        user=user,
        plan=plan,
        jurisdiction="illinois",
        current_step=WorkflowStepKey.REVIEW,
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        checklist_item_id="petition",
    )
    client.force_login(user)
    _prepare_submission(client, draft, monkeypatch)

    response = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    plan.refresh_from_db()
    assert plan.checklist["petition"]["status"] == "filed"


@pytest.mark.django_db
def test_confirmed_api_rejection_releases_draft_for_retry(client, django_user_model, monkeypatch):
    def fake_post(*_args, **_kwargs):
        return FakeApiResponse(400, {"error": "Rejected", "validation_errors": ["bad bundle"]})

    monkeypatch.setattr("requests.post", fake_post)
    user = django_user_model.objects.create_user(username="error-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", current_step=WorkflowStepKey.REVIEW)
    client.force_login(user)
    _prepare_submission(client, draft, monkeypatch)

    response = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
        content_type="application/json",
    )

    assert response.status_code == 400
    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.DRAFT


@pytest.mark.parametrize(
    ("status_code", "is_confirmed_rejection"),
    [(400, True), (422, True), (408, False), (409, False), (500, False)],
)
def test_confirmed_api_rejection_excludes_ambiguous_statuses(status_code, is_confirmed_rejection):
    from efile.views.submission import _confirmed_api_rejection

    assert _confirmed_api_rejection({"api_status_code": status_code}) is is_confirmed_rejection


@pytest.mark.django_db
def test_submission_claim_prevents_duplicate_filing(django_user_model):
    """The SUBMITTING claim is single-winner, so concurrent submits can't both file."""
    from efile.views.submission import _claim_for_submission

    user = django_user_model.objects.create_user(username="claim-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")

    assert _claim_for_submission(draft, {}) is True
    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.SUBMITTING
    # A second attempt on the same draft is refused.
    assert _claim_for_submission(draft, {}) is False


@pytest.mark.django_db
def test_ambiguous_submission_states_are_not_automatically_reclaimed(django_user_model):
    """SUBMITTING and ERROR require review because retrying either may double-file."""
    from efile.views.submission import _claim_for_submission

    user = django_user_model.objects.create_user(username="ambiguous-state-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")

    assert _claim_for_submission(draft, {}) is True
    assert _claim_for_submission(draft, {}) is False

    draft.mark_error({"error": "outcome unknown"})
    assert _claim_for_submission(draft, {}) is False


@pytest.mark.django_db
def test_precondition_failure_releases_claim_to_draft(client, django_user_model, monkeypatch):
    """A failure before the external call frees the draft for a safe retry."""
    user = django_user_model.objects.create_user(username="precondition-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", current_step=WorkflowStepKey.REVIEW)
    client.force_login(user)
    _prepare_submission(client, draft, monkeypatch)

    response = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft),  # no efile_data -> pre-call validation failure
        content_type="application/json",
    )

    assert response.status_code == 400
    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.DRAFT


@pytest.mark.django_db
def test_ambiguous_failure_does_not_release_to_draft(client, django_user_model, monkeypatch):
    """An error after requests.post may mean the filing went through: never reset to DRAFT."""

    calls = 0

    def boom(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("crashed after sending")

    monkeypatch.setattr("requests.post", boom)
    user = django_user_model.objects.create_user(username="ambiguous-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", current_step=WorkflowStepKey.REVIEW)
    client.force_login(user)
    _prepare_submission(client, draft, monkeypatch)

    response = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
        content_type="application/json",
    )

    assert response.status_code >= 400
    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.ERROR

    retry = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
        content_type="application/json",
    )

    assert retry.status_code == 409
    assert calls == 1


@pytest.mark.django_db
def test_route_jurisdiction_isolates_reads(client, django_user_model):
    """A request served for jurisdiction A must not read a draft pointed to from B."""
    from efile.utils.case_data_utils import get_case_data

    user = django_user_model.objects.create_user(username="multi-jur-user", tyler_jurisdiction="illinois")
    illinois_draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    massachusetts_draft = FilingDraft.objects.create(user=user, jurisdiction="massachusetts")
    write_case_data(illinois_draft, {"court": "cook:cd"})
    write_case_data(massachusetts_draft, {"court": "suffolk:ma"})

    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = massachusetts_draft.pk
    session.save()
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session

    assert get_case_data(request, jurisdiction="illinois") == {}


@pytest.mark.django_db
def test_model_step_choices_follow_workflow_registry():
    current_step = FilingDraft._meta.get_field("current_step")

    assert tuple(current_step.choices) == get_workflow_step_choices()


@pytest.mark.django_db
def test_partial_case_update_does_not_clear_omitted_fields(django_user_model):
    user = django_user_model.objects.create_user(username="partial-update-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        court_code="old-code",
        court_name="Court name to preserve",
    )

    write_case_data(draft, {"court": "new-code"})
    draft.refresh_from_db()

    assert draft.court_code == "new-code"
    assert draft.court_name == "Court name to preserve"


@pytest.mark.django_db
def test_payload_validation_failure_releases_draft_for_a_corrected_retry(client, django_user_model, monkeypatch):
    """A pre-flight rejection never reached the court, so the filer may fix and retry.

    The response carries a stable error code so changing the human-readable
    validation message cannot accidentally park the draft in ERROR.
    """

    def reject(*_args, **_kwargs):
        raise PayloadValidationError("This case type requires a party of every required type")

    def unreachable_post(*_args, **_kwargs):
        raise AssertionError("the filing API must not be called for an invalid payload")

    monkeypatch.setattr("efile.views.session_api.prepare_efile_payload", reject)
    monkeypatch.setattr("requests.post", unreachable_post)
    user = django_user_model.objects.create_user(username="invalid-payload-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", current_step=WorkflowStepKey.REVIEW)
    client.force_login(user)
    _prepare_submission(client, draft, monkeypatch)

    response = client.post(
        reverse("submit_final_filing"),
        data=accepted_submission(draft, efile_data={"al_court_bundle": [{}]}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "submission_payload_validation_failed"
    assert "requires a party of every required type" in response.json()["error"]
    draft.refresh_from_db()
    assert draft.status == FilingDraft.Status.DRAFT
