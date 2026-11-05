from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingParty
from efile.services.account_profile import cached_account_profile, default_state_code
from efile.services.current_drafts import ensure_current_draft
from efile.services.drafts import draft_snapshot
from efile.services.efsp_validation import party_validation, validate_party
from efile.workflow import WorkflowStepKey, continue_step, continue_url, get_workflow_context, return_target

# Account profile field -> the filer field it fills in when that field is blank.
_ACCOUNT_FIELDS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "address_line_1": "address",
    "address_line_2": "address_line2",
    "city": "city",
    "state": "state",
    "zip_code": "zip",
    "phone": "phone",
}


def _prefill_from_account(filer, profile, user):
    """Fill blank filer fields from the account profile, in memory only.

    Nothing is saved: these are defaults the filer is being asked to confirm,
    and they become real data when they submit the form. Returns whether the
    profile was usable, which the page uses to skip its own fetch.
    """
    for field, profile_key in _ACCOUNT_FIELDS.items():
        if not getattr(filer, field, ""):
            setattr(filer, field, profile.get(profile_key) or "")
    # The account's own email never comes back from the court's firm record --
    # it is the address the filer signed in with.
    if not filer.email:
        filer.email = getattr(user, "email", "") or ""
    return True


REQUIRED_CONTACT_FIELDS = {
    "first_name": "First name",
    "last_name": "Last name",
    "address_line_1": "Street address",
    "city": "City",
    "state": "State",
    "zip_code": "ZIP code",
    "email": "Email",
}


@require_http_methods(["GET", "POST"])
def your_information(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    draft = ensure_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.YOUR_INFORMATION,
        workflow_version=2,
    )
    filer, _created = FilingParty.objects.get_or_create(draft=draft, role="filer", sort_order=0)

    metadata = party_validation(jurisdiction, draft.court_code, filer.country or "US")
    field_errors = {}

    if request.method == "POST":
        required = REQUIRED_CONTACT_FIELDS
        values = {field: request.POST.get(field, "").strip() for field in required}
        values.update(
            {
                field: request.POST.get(field, "").strip()
                for field in ("middle_name", "suffix", "address_line_2", "phone")
            }
        )
        values["state"] = values["state"].upper()
        for field, value in values.items():
            setattr(filer, field, value)
        field_errors = validate_party(values, metadata, address_started=True)
        missing = [label for field, label in required.items() if not values[field]]
        if missing:
            messages.error(request, f"Complete these fields: {', '.join(missing)}.")
        elif not field_errors:
            filer.save()
            return_to = return_target(request)
            draft.current_step = continue_step(draft, return_to, WorkflowStepKey.PARTIES)
            draft.save(update_fields=["current_step", "updated_at"])
            return redirect(continue_url(draft, jurisdiction, return_to, WorkflowStepKey.PARTIES))

    # Fill the blanks from the filer's e-filing account before rendering, not
    # afterwards from JavaScript: the filer should not watch their own name
    # appear a second or two after the form does. Anything already saved on the
    # draft wins, so a filer who corrected an address keeps the correction.
    profile = cached_account_profile(request, jurisdiction) if request.method == "GET" else None
    if request.method == "GET":
        _prefill_from_account(filer, profile or {}, request.user)
    prefilled = bool(profile) or request.method == "POST"
    if request.method == "GET" and request.GET.get("edit") != "1" and not return_target(request):
        values = {
            field: getattr(filer, field, "")
            for field in (*REQUIRED_CONTACT_FIELDS, "phone", "middle_name", "suffix", "address_line_2")
        }
        if all(values[field] for field in REQUIRED_CONTACT_FIELDS) and not validate_party(
            values, metadata, address_started=True
        ):
            filer.save()
            draft.current_step = WorkflowStepKey.PARTIES
            draft.save(update_fields=["current_step", "updated_at"])
            return redirect(continue_url(draft, jurisdiction, "", WorkflowStepKey.PARTIES))

    context = {
        **metadata,
        "field_errors": field_errors,
        "selected_state": filer.state or default_state_code(jurisdiction),
        "unsupported_state": bool(filer.state) and filer.state not in dict(metadata["state_choices"]),
        "is_logged_in": True,
        "filing_draft": draft_snapshot(draft),
        "filer": filer,
        "return_to": return_target(request),
        "court_code": draft.court_code,
        "default_state_code": default_state_code(jurisdiction),
        # Tells the page it does not need to fetch the profile again.
        "profile_prefilled": prefilled,
    }
    context.update(get_workflow_context(WorkflowStepKey.YOUR_INFORMATION, jurisdiction, draft))
    return render(request, "efile/your_information.html", context)
