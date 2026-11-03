"""
Context processors for jurisdiction-aware templates
"""

from django.conf import settings

from .utils.config_loader import InvalidJurisdiction, config_loader


def jurisdiction_context(request):
    """Add current jurisdiction and its config to all template contexts."""

    # For templates, only use the jurisdiction if it is explicitly present in
    # the URL path (e.g. /jurisdiction/illinois/...) or GET params.
    # Do not bleed session jurisdiction into generic root routes like /about/ or /choose-jurisdiction.
    current_jurisdiction = None
    if request.GET.get("jurisdiction"):
        current_jurisdiction = request.GET.get("jurisdiction").lower()
    else:
        segments = request.path.split("/")
        if len(segments) >= 3 and segments[1] == "jurisdiction":
            current_jurisdiction = segments[2].lower()

    # Generic pages have no selected state. Do not pass absence through the
    # strict request-to-configuration boundary as a jurisdiction identifier.
    try:
        config = config_loader.load_jurisdiction_config(current_jurisdiction) if current_jurisdiction else {}
    except InvalidJurisdiction:
        # Error templates also run context processors. Invalid request input
        # must not prevent Django from rendering the original error response.
        config = {}

    return {
        "jurisdiction": current_jurisdiction,
        "config": config,
        "document_checklists_enabled": config.get("document_checklists_enabled", True) is not False,
    }


# Which user guide page explains each screen. Keys are URL names; values are
# page slugs (with an optional anchor) under settings.LITEFILE_USER_GUIDE_URL.
USER_GUIDE_PAGES = {
    "home": "can-i-file-here",
    "efile_choose_jurisdiction": "can-i-file-here",
    "jurisdiction_homepage": "can-i-file-here",
    "about": "overview",
    "jurisdiction_about": "overview",
    "efile_login": "sign-in",
    "efile_register": "create-account",
    "efile_password_reset": "sign-in#forgot-your-password",  # nosec B105 -- a guide page anchor, not a secret
    "efile_options": "start-filing",
    "filing_path": "start-filing",
    "start_filing": "start-filing",
    "find_documents": "before-you-begin",
    "find_documents_intent": "before-you-begin",
    "my_drafts": "save-and-return",
    "filing_plans": "save-and-return",
    "handoff_claim": "save-and-return#if-you-came-from-a-guided-interview",
    "handoff_review": "save-and-return#if-you-came-from-a-guided-interview",
    "handoff_replacements": "save-and-return#if-you-came-from-a-guided-interview",
    "upload_documents": "upload-documents",
    "preview_documents": "upload-documents",
    "extraction_review": "case-information",
    "case_filing_types": "case-information",
    "docket_court": "case-information",
    "case_lookup": "case-information#file-in-an-existing-case",
    "case_confirmation": "case-information#file-in-an-existing-case",
    "document_checklist": "organize-documents#check-your-documents",
    "organize_documents": "organize-documents",
    "your_information": "people",
    "parties": "people",
    "party_details": "people",
    "case_questions": "people",
    "payment": "fees",
    "waiver_documents": "fees#ask-for-a-fee-waiver",
    "case_review": "review-submit",
    "filing_confirmation": "after-you-file",
    "filing_statuses": "check-status",
    "filing_detail": "check-status",
    "submission_tracker": "check-status",
    "correct_filing": "fix-returned-filing",
}


def user_guide_context(request):
    """Link each screen to its page in the public user guide, if it has one."""
    match = getattr(request, "resolver_match", None)
    page = USER_GUIDE_PAGES.get(match.url_name) if match else None
    base = getattr(settings, "LITEFILE_USER_GUIDE_URL", "")
    if not page or not base:
        return {"user_guide_url": ""}
    return {"user_guide_url": base.rstrip("/") + "/" + page}
