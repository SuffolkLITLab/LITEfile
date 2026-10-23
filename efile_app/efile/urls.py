from django.conf import settings
from django.shortcuts import redirect
from django.urls import include, path
from django.views.i18n import JavaScriptCatalog

from efile.staff_admin import staff_site
from efile.utils.config_loader import config_loader
from efile.utils.jurisdiction_stuff import has_jurisdiction_login

from .views.api_views import get_case_data_api, get_filing_components
from .views.case_confirmation import case_confirmation
from .views.case_filing_types import case_filing_types
from .views.case_lookup import case_lookup, docket_court
from .views.case_questions import case_questions
from .views.choose_jurisdiction import change_jurisdiction, choose_jurisdiction
from .views.confirmation import filing_confirmation
from .views.document_checklist import document_checklist
from .views.document_checks import document_checks
from .views.document_finder import find_documents, find_documents_intent
from .views.document_previews import document_content, preview_documents
from .views.draft_views import get_current_draft_view, start_filing, start_filing_from_plan
from .views.extraction_review import extraction_review
from .views.filing_path import filing_path
from .views.filing_plans import filing_plans
from .views.handoff import (
    correct_filing,
    external_handoff,
    handoff_claim,
    handoff_review,
    replace_documents,
    return_to_interview,
)
from .views.handoff_replacements import handoff_replacements
from .views.legacy_workflow import legacy_workflow_redirect
from .views.login import efile_login, efile_logout, efile_password_reset
from .views.my_cases import filing_detail, filing_statuses
from .views.my_drafts import my_drafts
from .views.options import efile_options
from .views.organize_documents import organize_documents
from .views.parties import parties
from .views.party_details import party_details
from .views.payment import efile_payment
from .views.public_pages import about_page, terms_of_service_page
from .views.register import efile_register
from .views.review import case_review
from .views.session_api import (
    api_save_case_data,
    clear_session_data,
    fetch_and_save_party_type,
    get_upload_data_from_session,
    save_party_type_to_session,
)
from .views.submission import submit_final_filing
from .views.submission_tracker import submission_tracker
from .views.upload_documents import document_extraction_status, upload_documents
from .views.waiver_documents import waiver_documents
from .views.your_information import your_information


def homepage(request):
    return redirect("efile_choose_jurisdiction")


def jurisdiction_homepage(request, jurisdiction):
    if jurisdiction not in config_loader.get_available_jurisdictions():
        return redirect("efile_choose_jurisdiction")
    if has_jurisdiction_login(request, jurisdiction):
        return redirect("efile_options", jurisdiction)
    return redirect("efile_login", jurisdiction)


urlpatterns = [
    path("submissions/<uuid:public_id>/", submission_tracker, name="submission_tracker"),
    path(f"{settings.LITEFILE_STAFF_PATH}/", staff_site.urls),
    path("api/handoffs/v1/", external_handoff, name="external_handoff"),
    path("api/handoffs/v1/documents/", replace_documents, name="handoff_replace_documents"),
    path("handoff/claim/<str:token>/", handoff_claim, name="handoff_claim"),
    path("handoff/drafts/<int:draft_id>/", handoff_review, name="handoff_review"),
    path(
        "handoff/drafts/<int:draft_id>/replacements/<int:document_id>/",
        handoff_replacements,
        name="handoff_replacements",
    ),
    path("handoff/drafts/<int:draft_id>/correct/", correct_filing, name="correct_filing"),
    path("handoff/drafts/<int:draft_id>/interview/", return_to_interview, name="return_to_interview"),
    path("jsi18n/", JavaScriptCatalog.as_view(), name="javascript-catalog"),
    path("", homepage, name="home"),
    path("choose-jurisdiction", choose_jurisdiction, name="efile_choose_jurisdiction"),
    path("about/", about_page, name="about"),
    path("terms-of-service/", terms_of_service_page, name="terms_of_service"),
    path("jurisdiction/<jurisdiction>", jurisdiction_homepage, name="jurisdiction_homepage"),
    path("jurisdiction/<jurisdiction>/about/", about_page, name="jurisdiction_about"),
    path(
        "jurisdiction/<jurisdiction>/terms-of-service/",
        terms_of_service_page,
        name="jurisdiction_terms_of_service",
    ),
    path("jurisdiction/<jurisdiction>/login/", efile_login, name="efile_login"),
    path("jurisdiction/<jurisdiction>/logout/", efile_logout, name="efile_logout"),
    path(
        "jurisdiction/<jurisdiction>/change-jurisdiction/",
        change_jurisdiction,
        name="change_jurisdiction",
    ),
    path("jurisdiction/<jurisdiction>/register/", efile_register, name="efile_register"),
    path("jurisdiction/<jurisdiction>/password_reset/", efile_password_reset, name="efile_password_reset"),
    path("jurisdiction/<jurisdiction>/options/", efile_options, name="efile_options"),
    path("jurisdiction/<jurisdiction>/filing-path/", filing_path, name="filing_path"),
    path("jurisdiction/<jurisdiction>/waiver-documents/", waiver_documents, name="waiver_documents"),
    path("jurisdiction/<jurisdiction>/document-checks/", document_checks, name="document_checks"),
    path("jurisdiction/<jurisdiction>/upload-documents/", upload_documents, name="upload_documents"),
    path("jurisdiction/<jurisdiction>/preview-documents/", preview_documents, name="preview_documents"),
    path("jurisdiction/<jurisdiction>/documents/<int:document_id>/content/", document_content, name="document_content"),
    path(
        "jurisdiction/<jurisdiction>/document-extraction-status/",
        document_extraction_status,
        name="document_extraction_status",
    ),
    path("jurisdiction/<jurisdiction>/extraction-review/", extraction_review, name="extraction_review"),
    path("jurisdiction/<jurisdiction>/case-filing-types/", case_filing_types, name="case_filing_types"),
    path("jurisdiction/<jurisdiction>/docket-court/", docket_court, name="docket_court"),
    path("jurisdiction/<jurisdiction>/case-lookup/", case_lookup, name="case_lookup"),
    path("jurisdiction/<jurisdiction>/case-confirmation/", case_confirmation, name="case_confirmation"),
    path("jurisdiction/<jurisdiction>/document-checklist/", document_checklist, name="document_checklist"),
    path("jurisdiction/<jurisdiction>/organize-documents/", organize_documents, name="organize_documents"),
    path("jurisdiction/<jurisdiction>/your-information/", your_information, name="your_information"),
    path("jurisdiction/<jurisdiction>/parties/", parties, name="parties"),
    path("jurisdiction/<jurisdiction>/party-details/", party_details, name="party_details"),
    path("jurisdiction/<jurisdiction>/case-questions/", case_questions, name="case_questions"),
    path("jurisdiction/<jurisdiction>/plans/", filing_plans, name="filing_plans"),
    path("jurisdiction/<jurisdiction>/find-documents/", find_documents, name="find_documents"),
    path(
        "jurisdiction/<jurisdiction>/find-documents/<slug:intent_id>/",
        find_documents_intent,
        name="find_documents_intent",
    ),
    path(
        "jurisdiction/<jurisdiction>/plans/<int:plan_id>/filings/",
        start_filing_from_plan,
        name="start_filing_from_plan",
    ),
    path("jurisdiction/<jurisdiction>/filing_statuses/", filing_statuses, name="filing_statuses"),
    path(
        "jurisdiction/<jurisdiction>/filings/<court_code>/<filing_id>/",
        filing_detail,
        name="filing_detail",
    ),
    path("jurisdiction/<jurisdiction>/my-drafts/", my_drafts, name="my_drafts"),
    path("jurisdiction/<jurisdiction>/start-filing/", start_filing, name="start_filing"),
    path(
        "jurisdiction/<jurisdiction>/expert_form/",
        legacy_workflow_redirect,
        {"destination": "expert_form"},
        name="expert_form",
    ),
    path(
        "jurisdiction/<jurisdiction>/upload_first/",
        legacy_workflow_redirect,
        {"destination": "upload_first"},
        name="upload_first",
    ),
    path(
        "jurisdiction/<jurisdiction>/upload/",
        legacy_workflow_redirect,
        {"destination": "upload"},
        name="upload",
    ),
    path("jurisdiction/<jurisdiction>/payment/", efile_payment, name="payment"),
    path("jurisdiction/<jurisdiction>/review/", case_review, name="case_review"),
    path("jurisdiction/<jurisdiction>/filing-confirmation/", filing_confirmation, name="filing_confirmation"),
    # Session API endpoints
    path("api/get-case-data/", get_case_data_api, name="get_case_data_api"),
    path("api/get-filing-components/", get_filing_components, name="get_filing_components"),
    path("api/draft/", get_current_draft_view, name="get_current_draft"),
    path("api/save-case-data/", api_save_case_data, name="save_case_data_api"),
    path("api/get-upload-data/", get_upload_data_from_session, name="get_upload_data_from_session"),
    path("api/fetch-party-type/", fetch_and_save_party_type, name="fetch_party_type"),
    path("api/save-party-type/", save_party_type_to_session, name="save_party_type"),
    path("api/submit-final-filing/", submit_final_filing, name="submit_final_filing"),
    path("api/clear-session/", clear_session_data, name="clear_session_data"),
    # API endpoints for dropdowns
    path("api/", include("efile.api.urls")),
    # Legacy endpoints for backward compatibility (can be removed later)
    # path('api/get-case-categories/', views.efile_logout, name='get_case_categories_legacy'),
    # path('api/get-case-types/', views.efile_logout, name='get_case_types_legacy'),
    # path('api/get-filing-types/', views.efile_logout, name='get_filing_types_legacy'),
    # path('api/get-counties/', views.efile_logout, name='get_counties_legacy'),
    # path('api/get-document-types/', views.efile_logout, name='get_document_types_legacy'),
]

if settings.CROSSWALK_REVIEW_ENABLED:
    urlpatterns.append(path("review/", include("crosswalk_review.urls")))
