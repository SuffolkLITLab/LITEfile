import pytest
from django.urls import reverse

from efile.models import FilingDocument, FilingDraft, FilingParty
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.fee_quotes import record_fee_quote
from efile.tests.helpers import reviewed_document
from efile.workflow import WorkflowStepKey


@pytest.fixture
def submission_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="review-user", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        current_step=WorkflowStepKey.PAYMENT,
        existing_case="new",
        court_code="cook:law1",
        court_name="Circuit Court of Cook County",
        case_category_code="civil",
        case_category_name="Civil",
        case_type_code="contract",
        case_type_name="Contract",
        document_checklist_acknowledged=True,
    )
    reviewed_document(
        draft=draft,
        role=FilingDocument.Role.LEAD,
        sort_order=0,
        name="Petition.pdf",
        filing_type_code="petition",
        filing_type_name="Petition",
        document_type_code="public",
        document_type_name="Public",
        filing_component_code="lead",
        filing_component_name="Lead document",
    )
    FilingParty.objects.create(
        draft=draft,
        role="filer",
        sort_order=0,
        party_type="PLA",
        party_type_name="Plaintiff/Petitioner",
        first_name="Jordan",
        last_name="Taylor",
        email="jordan@example.com",
        address_line_1="123 Main Street",
        city="Springfield",
        state="IL",
        zip_code="62701",
    )
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "test-token"}
    session.save()
    return draft


@pytest.mark.django_db
def test_payment_saves_account_and_advances_durable_step(client, submission_draft, monkeypatch):
    monkeypatch.setattr(
        "efile.views.payment.payment_accounts",
        lambda *args: [{"paymentAccountID": "pay-123", "accountName": "Saved account", "paymentAccountTypeCode": "CC"}],
    )
    response = client.post(
        reverse("payment", kwargs={"jurisdiction": "illinois"}),
        {"selected_payment_account": "pay-123", "selected_payment_account_name": "Card ending in 4242"},
    )

    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})
    submission_draft.refresh_from_db()
    assert submission_draft.selected_payment_account_id == "pay-123"
    assert submission_draft.current_step == WorkflowStepKey.REVIEW


@pytest.mark.django_db
def test_final_review_marks_extracted_values_and_explains_the_marker(client, submission_draft):
    submission_draft.case_title = "Jordan Taylor v. Acme"
    submission_draft.docket_number = "2026-CV-123"
    submission_draft.extracted_guesses = {
        "case title": "Jordan Taylor v. Acme",
        "docket number": "2026-CV-123",
        "court": "Circuit Court of Cook County",
        "case category": "Civil",
        "case type": "Contract",
        "filing type": "Petition",
    }
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.selected_payment_account_name = "Card ending in 4242"
    submission_draft.save(
        update_fields=[
            "case_title",
            "docket_number",
            "extracted_guesses",
            "selected_payment_account_id",
            "selected_payment_account_name",
            "updated_at",
        ]
    )

    response = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"}))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Circuit Court of Cook County *" in content
    assert "A * marks an answer found in your document." in content
    assert 'data-bs-target="#extraction-marker-modal"' in content


def _case_card(content):
    return content.partition('<section class="review-summary-card">')[2].partition("</section>")[0]


@pytest.mark.django_db
@pytest.mark.parametrize("supplemental_fields", [{}, {"has_children": False, "_case_questions_required": True}])
def test_final_review_shows_amount_with_a_case_questions_edit_link(client, submission_draft, supplemental_fields):
    submission_draft.amount_in_controversy = "1234.50"
    submission_draft.supplemental_fields = supplemental_fields
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.save()

    response = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"}))
    content = response.content.decode()
    questions_card = content.partition("<h2>Case questions</h2>")[2].partition("</section>")[0]

    assert response.status_code == 200
    assert "Amount in controversy" in questions_card
    assert "$1234.50" in questions_card
    assert reverse("case_questions", kwargs={"jurisdiction": "illinois"}) + "?return_to=review" in questions_card
    if supplemental_fields:
        assert "Has Children" in questions_card
        assert "No" in questions_card
        assert "_case_questions_required" not in questions_card


@pytest.mark.django_db
def test_final_review_omits_empty_case_questions(client, submission_draft):
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.supplemental_fields = {"_case_questions_required": True}
    submission_draft.save()

    response = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert b"Amount in controversy" not in response.content
    assert b"<h2>Case questions</h2>" not in response.content


@pytest.mark.django_db
def test_amount_edit_returns_to_review_with_the_saved_value(client, submission_draft):
    submission_draft.amount_in_controversy = "1234.50"
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.save()
    submission_draft.documents.update(filing_requires_amount_in_controversy=True)
    edit_url = reverse("case_questions", kwargs={"jurisdiction": "illinois"}) + "?return_to=review"

    edit_response = client.get(edit_url)
    assert edit_response.status_code == 200
    assert b'value="1234.50"' in edit_response.content

    response = client.post(edit_url, {"amount_in_controversy": "$2,345.67", "return_to": "review"})

    assert response.status_code == 302
    assert response.url.partition("?")[0] == reverse("case_review", kwargs={"jurisdiction": "illinois"})
    response = client.get(response.url)
    assert response.status_code == 200
    assert b"$2345.67" in response.content
    assert b"$1234.50" not in response.content
    submission_draft.refresh_from_db()
    assert submission_draft.amount_in_controversy == "2345.67"


@pytest.mark.django_db
def test_final_review_hides_case_title_and_number_for_a_new_case(client, submission_draft):
    # Saved by an older version of the confirm-case screen.
    submission_draft.case_title = "Jordan Taylor v. Acme"
    submission_draft.docket_number = "2026-CV-123"
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.save(update_fields=["case_title", "docket_number", "selected_payment_account_id", "updated_at"])

    content = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"})).content.decode()
    case_card = _case_card(content)

    assert "Jordan Taylor v. Acme" not in case_card
    assert "2026-CV-123" not in case_card


@pytest.mark.django_db
def test_final_review_shows_a_found_case_as_the_courts_read_only_record(client, submission_draft):
    submission_draft.existing_case = "existing"
    submission_draft.previous_case_id = "tracking-1"
    submission_draft.case_title = "Jordan Taylor v. Acme"
    submission_draft.docket_number = "2026-CV-123"
    submission_draft.extracted_guesses = {"case title": "Jordan Taylor v. Acme", "docket number": "2026-CV-123"}
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.save()

    from efile.tests.helpers import loaded_case_snapshot

    submission_draft.existing_case_snapshot = loaded_case_snapshot(submission_draft)
    submission_draft.save(update_fields=["existing_case_snapshot"])

    content = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"})).content.decode()
    case_card = _case_card(content)

    assert "Jordan Taylor v. Acme" in case_card
    assert "2026-CV-123" in case_card
    assert "From the court" in case_card
    # The court's values, not something we read from the document.
    assert "Jordan Taylor v. Acme *" not in case_card
    assert "2026-CV-123 *" not in case_card


class _PaymentAccountTypesResponse:
    status_code = 200

    @staticmethod
    def json():
        return [
            {"code": "CC", "description": "Credit Card"},
            {"code": "WV", "description": "Waiver"},
        ]


@pytest.mark.django_db
def test_payment_account_types_proxies_the_courts_type_list(client, submission_draft, monkeypatch):
    monkeypatch.setattr(
        "efile.api.auth_views.requests.get",
        lambda *args, **kwargs: _PaymentAccountTypesResponse(),
    )

    response = client.get(reverse("api:payment_account_types"), {"jurisdiction": "illinois"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert {"code": "CC", "description": "Credit Card"} in body["data"]


@pytest.mark.django_db
def test_payment_persists_account_type_but_not_a_posted_fee_total(client, submission_draft, monkeypatch):
    """The quote comes from the fee API, which recorded what it priced; a form field cannot set it."""
    monkeypatch.setattr(
        "efile.views.payment.payment_accounts",
        lambda *args: [{"paymentAccountID": "pay-123", "accountName": "Saved account", "paymentAccountTypeCode": "CC"}],
    )
    response = client.post(
        reverse("payment", kwargs={"jurisdiction": "illinois"}),
        {
            "selected_payment_account": "pay-123",
            "selected_payment_account_name": "Card ending in 4242",
            "selected_payment_account_type": "CC",
            "quoted_fee_total": "1.00",
            "quoted_fee_breakdown": '[{"label": "Filing fee", "amount": "1.00"}]',
        },
    )

    assert response.status_code == 302
    submission_draft.refresh_from_db()
    assert submission_draft.selected_payment_account_type == "CC"
    assert submission_draft.quoted_fee_total == ""


@pytest.mark.django_db
def test_payment_tolerates_malformed_fee_breakdown(client, submission_draft, monkeypatch):
    monkeypatch.setattr(
        "efile.views.payment.payment_accounts",
        lambda *args: [{"paymentAccountID": "pay-123", "accountName": "Saved account", "paymentAccountTypeCode": "WV"}],
    )
    response = client.post(
        reverse("payment", kwargs={"jurisdiction": "illinois"}),
        {
            "selected_payment_account": "pay-123",
            "selected_payment_account_name": "Payment waiver",
            "selected_payment_account_type": "WV",
            "quoted_fee_breakdown": "not json",
        },
    )

    assert response.status_code == 302
    submission_draft.refresh_from_db()
    assert submission_draft.selected_payment_account_type == "WV"
    assert submission_draft.quoted_fee_breakdown == []


@pytest.mark.django_db
def test_review_shows_waiver_messaging_instead_of_fee_reference(client, submission_draft):
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.selected_payment_account_name = "Payment waiver"
    submission_draft.selected_payment_account_type = "WV"
    submission_draft.save(
        update_fields=["selected_payment_account_id", "selected_payment_account_name", "selected_payment_account_type"]
    )

    response = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert b"Fee waiver requested" in response.content
    assert b"approved fee waiver" not in response.content
    assert b"the previous screen" not in response.content


@pytest.mark.django_db
def test_review_shows_previously_calculated_fee_total(client, submission_draft):
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.selected_payment_account_name = "Card ending in 4242"
    submission_draft.selected_payment_account_type = "CC"
    submission_draft.save(
        update_fields=["selected_payment_account_id", "selected_payment_account_name", "selected_payment_account_type"]
    )
    record_fee_quote(submission_draft, "125.00", [{"label": "Filing fee", "amount": "125.00"}])

    response = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert b"125.00" in response.content
    assert b"Filing fee" in response.content
    assert b"the previous screen" not in response.content


@pytest.mark.django_db
def test_review_uses_new_edit_routes_and_durable_summary(client, submission_draft):
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.selected_payment_account_name = "Card ending in 4242"
    submission_draft.save(update_fields=["selected_payment_account_id", "selected_payment_account_name"])

    response = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert b"Jordan" in response.content
    assert b"Petition.pdf" in response.content
    assert b"Filing code:" in response.content
    assert b"review-document-tag" in response.content
    assert reverse("organize_documents", kwargs={"jurisdiction": "illinois"}).encode() in response.content
    assert reverse("your_information", kwargs={"jurisdiction": "illinois"}).encode() in response.content
    assert f'href="{reverse("expert_form", kwargs={"jurisdiction": "illinois"})}'.encode() not in response.content
    assert f'href="{reverse("upload", kwargs={"jurisdiction": "illinois"})}'.encode() not in response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("route", "target"),
    [
        ("upload_first", "upload_documents"),
        ("expert_form", "extraction_review"),
        ("upload", "organize_documents"),
    ],
)
def test_retired_screen_urls_redirect_forward(client, submission_draft, route, target):
    response = client.get(reverse(route, kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 302
    # The filing is named in the URL: following an old link carries on with that
    # filing rather than leaving the next screen to guess which one it is.
    assert response.url == (reverse(target, kwargs={"jurisdiction": "illinois"}) + f"?draft={submission_draft.pk}")


@pytest.mark.django_db
def test_confirmation_uses_saved_submission_reference(client, submission_draft):
    submission_draft.mark_submitted({"confirmationNumber": "IL-2026-12345"})
    session = client.session
    session["last_submitted_filing_draft_id"] = submission_draft.pk
    session.save()

    response = client.get(reverse("filing_confirmation", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    assert b"IL-2026-12345" in response.content
    assert b"Circuit Court of Cook County" in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("total,stale,visible", [("0.00", False, True), ("10.00", False, False), ("0.00", True, False)])
def test_free_filing_explanation_requires_current_zero_quote(client, submission_draft, total, stale, visible):
    submission_draft.selected_payment_account_id = "pay-123"
    submission_draft.selected_payment_account_type = "CC"
    submission_draft.save()
    record_fee_quote(submission_draft, total, [{"label": "Test fee item", "amount": total}])
    if stale:
        submission_draft.amount_in_controversy = "999"
        submission_draft.save()
    response = client.get(reverse("case_review", kwargs={"jurisdiction": "illinois"}))
    assert response.status_code == 200
    assert response.context["fee_quote"]["is_zero"] is visible
    body = response.content.decode()
    help_tag = body.split('id="free-filing-help"', 1)[1].split(">", 1)[0]
    assert ("hidden" not in help_tag) is visible
    assert 'class="info-explainer"' in body
    assert 'id="free-filing-explainer-content" hidden' in body
    assert "Your confirmed total is $0, so you will not be charged." in body
    assert ("Test fee item" in body) is (not visible and not stale)
    assert ("Submit and pay" in body) is (total != "0.00" and not stale)
