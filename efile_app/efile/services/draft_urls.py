"""URLs that belong to a particular filing, shared with the browser."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.urls import Resolver404, resolve, reverse

WORKFLOW_VIEWS = frozenset(
    {
        "filing_path",
        "upload_documents",
        "preview_documents",
        "document_extraction_status",
        "extraction_review",
        "case_lookup",
        "case_confirmation",
        "document_checklist",
        "organize_documents",
        "your_information",
        "parties",
        "party_details",
        "case_questions",
        "service",
        "update_service_contacts",
        "payment",
        "waiver_documents",
        "document_checks",
        "case_review",
        "filing_confirmation",
        "expert_form",
        "upload_first",
        "upload",
    }
)


def draft_url(url, draft_id):
    """Carry identity through workflow redirects, preserving other query parameters."""
    parts = urlsplit(url)
    if parts.netloc or parts.scheme:
        return url
    try:
        if resolve(parts.path).url_name not in WORKFLOW_VIEWS:
            return url
    except Resolver404:
        return url
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("draft", str(draft_id))
    return urlunsplit(parts._replace(query=urlencode(query)))


def browser_draft_context(request):
    draft = getattr(request, "filing_draft", None)
    if draft is None:
        return {}
    from efile.services.handoff import receipt_for

    return {
        "handoff_draft_id": draft.pk if receipt_for(draft) else None,
        "draft_scope": {
            "id": draft.pk,
            "paths": [reverse(name, kwargs={"jurisdiction": draft.jurisdiction}) for name in sorted(WORKFLOW_VIEWS)],
        },
    }
