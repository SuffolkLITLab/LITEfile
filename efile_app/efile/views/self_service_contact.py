"""Owner-only registration for receiving future service after a filing."""

from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDraft
from efile.services.self_service_contact import case_id, register_contact
from efile.services.service_contacts import ServiceContactError
from efile.utils.jurisdiction_stuff import redirect_to_jurisdiction_login


@require_http_methods(["GET", "POST"])
def self_service_contact(request, public_id):
    if not request.user.is_authenticated:
        draft = get_object_or_404(FilingDraft.objects.only("jurisdiction"), public_id=public_id)
        return redirect_to_jurisdiction_login(request, draft.jurisdiction)
    draft = get_object_or_404(
        FilingDraft,
        public_id=public_id,
        status=FilingDraft.Status.SUBMITTED,
        user=request.user,
        deletion_pending=False,
    )
    token = get_tyler_token(request, draft.jurisdiction)
    if not token:
        return redirect_to_jurisdiction_login(request, draft.jurisdiction)
    error = ""
    registered = False
    first = request.POST.get("first_name", request.user.first_name)
    last = request.POST.get("last_name", request.user.last_name)
    if request.method == "POST":
        try:
            if request.POST.get("consent") != "yes":
                raise ServiceContactError(
                    "Confirm that you want to receive future service at your account email address."
                )
            register_contact(draft, token, first, last)
            registered = True
        except (ServiceContactError, ValueError) as exc:
            error = str(exc)
    return render(
        request,
        "efile/self_service_contact.html",
        {
            "draft": draft,
            "jurisdiction": draft.jurisdiction,
            "is_logged_in": True,
            "registered": registered,
            "case_ready": bool(case_id(draft)),
            "registration_error": error,
            "first_name": first,
            "last_name": last,
            "account_email": request.user.email,
        },
    )
