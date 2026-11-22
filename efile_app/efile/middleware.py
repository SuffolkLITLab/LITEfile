from django.contrib import messages
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.deprecation import MiddlewareMixin

from efile.models import FilingDraft
from efile.services.current_drafts import (
    DraftIdentityError,
    explicit_draft_id,
    get_current_draft,
    resolve_explicit_draft,
)
from efile.services.draft_urls import draft_url
from efile.services.drafts import SUBMISSION_OUTCOME_STATUSES
from efile.services.existing_cases import import_ready
from efile.utils.jurisdiction_stuff import get_jurisdiction_from_request
from efile.workflow import ExistingCase

# Steps that read or file the court roster. An existing case reaches them only
# once its import is loaded and confirmed; this is the one place that says so.
NEEDS_CONFIRMED_CASE = {"parties", "party_details", "case_questions", "service", "payment", "case_review"}


def _submission_pending(request) -> bool:
    """Whether the named draft is this filer's, cut off while being sent (#445).

    Such a draft can no longer be opened, and the filing may already be with
    the court, so the page must not suggest starting the filing again.
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return False
    try:
        draft_id = explicit_draft_id(request)
    except DraftIdentityError:
        return False
    return (
        draft_id is not None
        and FilingDraft.objects.filter(pk=draft_id, user=user, status=FilingDraft.Status.SUBMITTING).exists()
    )


class DraftIdentityMiddleware(MiddlewareMixin):
    """Validate named drafts before views run and preserve them in redirects."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.resolver_match.namespace == "litefile_staff":
            return None
        statuses = (FilingDraft.Status.DRAFT, FilingDraft.Status.ERROR)
        if request.resolver_match.url_name == "submit_final_filing":
            statuses = (*statuses, FilingDraft.Status.SUBMITTING)
        if request.resolver_match.url_name == "filing_confirmation":
            statuses = (FilingDraft.Status.SUBMITTED,)
        if request.resolver_match.url_name == "submission_status":
            statuses = SUBMISSION_OUTCOME_STATUSES
        try:
            jurisdiction = view_kwargs.get("jurisdiction")
            draft = resolve_explicit_draft(request, jurisdiction=jurisdiction, statuses=statuses)
            if request.resolver_match.url_name in NEEDS_CONFIRMED_CASE:
                draft = draft or get_current_draft(request, jurisdiction=jurisdiction)
                if draft and draft.existing_case == ExistingCase.EXISTING and not import_ready(draft):
                    messages.error(request, "Confirm the court case and load its parties before continuing.")
                    return redirect("case_confirmation", jurisdiction=draft.jurisdiction)
        except DraftIdentityError as error:
            return self.process_exception(request, error)

    def process_exception(self, request, exception):
        if isinstance(exception, DraftIdentityError):
            if (
                not request.path.startswith("/api/")
                and "application/json" not in request.headers.get("Accept", "")
                and request.content_type != "application/json"
            ):
                return render(
                    request,
                    "efile/draft_unavailable.html",
                    {"submission_pending": _submission_pending(request)},
                    status=409,
                )
            if request.resolver_match.url_name in {"upload_documents", "document_extraction_status"} and (
                not request.user.is_authenticated
            ):
                return JsonResponse({"success": False, "error": "Sign in again to continue."}, status=401)
            return JsonResponse(
                {
                    "success": False,
                    "error_code": "draft_unavailable",
                    "error": "This draft is no longer available here. Open My drafts to choose a filing, or start a new one.",
                },
                status=409,
            )

    def process_response(self, request, response):
        draft = getattr(request, "filing_draft", None)
        if draft is not None:
            # Where a step goes next, including back to a detour's origin, is
            # the step's own decision (efile.workflow.continue_url). This only
            # keeps the filing named in the URL it chose.
            if response.has_header("Location"):
                response["Location"] = draft_url(response["Location"], draft.pk)
            elif isinstance(response, JsonResponse):
                import json

                payload = json.loads(response.content)
                if isinstance(payload, dict) and isinstance(payload.get("redirect_url"), str):
                    payload["redirect_url"] = draft_url(payload["redirect_url"], draft.pk)
                    response.content = json.dumps(payload)
        return response


class JurisdictionSessionMiddleware:
    """End an authenticated session before it can cross a jurisdiction boundary."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        jurisdiction = get_jurisdiction_from_request(request)
        user = getattr(request, "user", None)
        account_jurisdiction = getattr(user, "tyler_jurisdiction", "")
        if (
            jurisdiction
            and getattr(user, "is_authenticated", False)
            and account_jurisdiction
            and account_jurisdiction.casefold() != jurisdiction.casefold()
        ):
            logout(request)

        return self.get_response(request)


class NoCacheHTMLMiddleware:
    """Stop browsers from reusing a stale rendered page without asking the server first.

    Django's views send no Cache-Control at all, which leaves the browser free to
    apply its own heuristic freshness and serve an old page byte-for-byte on a plain
    reload -- no request even reaches the server. That's the same failure mode
    WHITENOISE_MAX_AGE=0 (settings_dev.py) fixes for static JS/CSS, but it was still
    open for the HTML itself: a template change (e.g. a <select> replaced with radio
    inputs) could sit invisible until the user did a hard refresh.

    Runs after WhiteNoise in MIDDLEWARE, so it never touches the responses WhiteNoise
    already serves (and already marks with its own Cache-Control) -- only the actual
    Django view responses, which is exactly what has no header set today. These pages
    also carry per-session case data, so not caching them is correct beyond just
    dev convenience.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Cache-Control" not in response:
            response["Cache-Control"] = "no-store"
        return response
