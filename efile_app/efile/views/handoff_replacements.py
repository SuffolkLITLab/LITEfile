"""Focused code lookup for an imported draft; existing selections stay fixed."""

from urllib.parse import urlencode

import requests
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from efile.services.filing_code_search import current_index, search_paths
from efile.services.handoff import HandoffError, effective_hints, receipt_for, resolve_metadata
from efile.services.handoff_replacements import apply_replacement, compatible, locked_choices
from efile.views.handoff import _owned, _private
from efile.workflow import ExistingCase

CHOICE_LABELS = (
    ("court", "Court"),
    ("case_category", "Case category"),
    ("case_type", "Case type"),
    ("filing_type", "Filing type"),
)


@require_http_methods(["GET", "POST"])
def handoff_replacements(request, draft_id, document_id):
    try:
        draft = _owned(request, draft_id)
    except HandoffError as exc:
        return _private(render(request, "efile/handoff_replacements.html", {"error": str(exc)}, status=exc.status))
    document = get_object_or_404(draft.documents, pk=document_id)
    if draft.status != "draft" or draft.existing_case != ExistingCase.NEW:
        return redirect("handoff_review", draft_id=draft.pk)
    receipt = receipt_for(draft)
    if not receipt:
        return redirect("handoff_review", draft_id=draft.pk)
    if all(locked_choices(draft, document).values()):
        return redirect("handoff_review", draft_id=draft.pk)
    suggestion = draft.metadata_events.filter(path=f"documents.{document.pk}", kind="source_suggestion").first()
    hints = effective_hints(
        receipt.payload, court_name=draft.court_name, document=suggestion.value if suggestion else {}
    )
    top_hints = effective_hints(receipt.payload, court_name=draft.court_name)
    candidates = hints.get("filing_type_name_hints", []) or (
        top_hints.get("filing_type_name_hints", []) if document.role == "lead" else []
    )
    if document.filing_type_code:
        candidates = (
            top_hints.get("case_type_name_hints", [])
            if not draft.case_type_code
            else top_hints.get("case_category_name_hints", [])
        )
    default_query = next((hint for hint in candidates if hint.strip()), document.name)[:160]
    query = request.GET.get("q", default_query).strip()[:160]
    try:
        offset = max(0, min(int(request.GET.get("offset", "0")), 10000))
    except ValueError:
        offset = 0
    index = current_index(draft.jurisdiction)
    error = ""
    status = 200
    results = []
    total = 0
    if request.method == "POST":
        try:
            submitted_id = request.POST.get("path_id", "")
            if not submitted_id.isascii() or not submitted_id.isdecimal() or len(submitted_id) > 18:
                raise HandoffError("Choose an available replacement.", status=400)
            path_id = int(submitted_id)
            path = (
                index.paths.filter(pk=path_id, initial=draft.existing_case == ExistingCase.NEW).first()
                if index
                else None
            )
            if path is None:
                raise HandoffError("This option is no longer in the lookup. Search again.", status=409)
            if not compatible(
                draft,
                document,
                {**{key: getattr(path, key) for key in locked_choices(draft, document)}, "initial": path.initial},
            ):
                raise HandoffError("This option would change a saved choice. Nothing was changed.", status=409)
            apply_replacement(draft, document, path)
            draft.refresh_from_db()
            resolve_metadata(draft)
            return redirect("handoff_review", draft_id=draft.pk)
        except (ValueError, HandoffError) as exc:
            error, status = str(exc), getattr(exc, "status", 409)
        except requests.RequestException:
            error, status = "The court list could not be checked. Your draft is saved; try again later.", 503
    if request.method == "POST":
        try:
            draft = _owned(request, draft_id)
        except HandoffError as exc:
            return _private(render(request, "efile/handoff_replacements.html", {"error": str(exc)}, status=exc.status))
        document = draft.documents.filter(pk=document_id).first()
        if document is None or draft.status != "draft" or draft.existing_case != ExistingCase.NEW:
            return _private(
                render(
                    request,
                    "efile/handoff_replacements.html",
                    {"draft": draft, "error": error, "recovery_only": True},
                    status=status,
                )
            )
    if index and query:
        result = search_paths(
            index,
            query,
            offset=offset,
            initial=draft.existing_case == ExistingCase.NEW,
            court=draft.court_code,
            category=draft.case_category_code,
            case_type=draft.case_type_code,
            filing_type=document.filing_type_code,
        )
        results = result["results"]
        total = result["total"]
    saved = [
        {
            "label": label,
            "value": getattr(draft if facet != "filing_type" else document, f"{facet}_name")
            or locked_choices(draft, document)[facet],
        }
        for facet, label in CHOICE_LABELS
        if locked_choices(draft, document)[facet]
    ]
    for result in results:
        result["changes"] = [
            {"label": label, "value": result[facet]["name"]}
            for facet, label in CHOICE_LABELS
            if not locked_choices(draft, document)[facet]
        ]
    return _private(
        render(
            request,
            "efile/handoff_replacements.html",
            {
                "draft": draft,
                "document": document,
                "query": query,
                "results": results,
                "saved": saved,
                "error": error,
                "lookup_available": index is not None,
                "total": total,
                "next_results": "?" + urlencode({"q": query, "offset": offset + 20})
                if offset + 20 < total and offset < 10000
                else "",
            },
            status=status,
        )
    )
