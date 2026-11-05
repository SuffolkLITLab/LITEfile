from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingParty
from efile.party_sides import PartySide, side_for_party_type_name
from efile.services.current_drafts import ensure_current_draft
from efile.services.drafts import draft_snapshot
from efile.services.efsp_validation import party_validation, validate_party
from efile.services.extracted_parties import party_display_name
from efile.services.party_requirements import address_is_blank, party_address_requirement
from efile.services.people import (
    claim_replaces_a_name,
    filer_is_party,
    get_case_questions,
    get_party_types,
    incomplete_parties,
    needs_amount_in_controversy,
    party_can_be_the_filer,
)
from efile.workflow import (
    WorkflowStepKey,
    continue_step,
    continue_url,
    get_workflow_context,
    return_target,
    with_return_to,
)


@require_http_methods(["GET", "POST"])
def party_details(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    draft = ensure_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.PARTY_DETAILS,
        workflow_version=2,
    )
    party = get_object_or_404(FilingParty, draft=draft, role="other", pk=request.GET.get("party"))
    if party.source == "court":
        messages.error(request, "Party details already on the court case cannot be changed here.")
        return redirect("parties", jurisdiction=jurisdiction)
    filer_row = FilingParty.objects.filter(draft=draft, role="filer").first()
    party_types = get_party_types(draft)
    party_type_names = {item["code"]: item["name"] for item in party_types}
    show_optional_address = not address_is_blank(party)
    # The filer's own court role, if they are a party. Another party can share
    # it -- joint petitioners, co-plaintiffs -- but far more often choosing it
    # is a slip for the other side's role, so it is offered with a question.
    filer_party_type = filer_row.party_type if filer_row else ""
    same_role_error = False

    metadata = party_validation(jurisdiction, draft.court_code, party.country or "US")
    field_errors = {}

    if request.method == "POST":
        party_kind = request.POST.get("party_kind", "person")
        party_type = request.POST.get("party_type", "").strip()
        if party_type and party_type not in party_type_names:
            party_type = ""
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        organization_name = request.POST.get("organization_name", "").strip()
        address = {
            "address_line_1": request.POST.get("address_line_1", "").strip(),
            "city": request.POST.get("city", "").strip(),
            "state": request.POST.get("state", "").strip().upper(),
            "zip_code": request.POST.get("zip_code", "").strip(),
        }
        address_line_2 = request.POST.get("address_line_2", "").strip()
        selected_party = FilingParty(
            draft=draft,
            party_type=party_type,
            party_type_name=party_type_names.get(party_type, ""),
        )
        address_requirement = party_address_requirement(draft, selected_party, party_types=party_types)
        address_started = any(address.values()) or bool(address_line_2)
        address_complete = all(address.values())
        show_optional_address = address_started
        has_name = organization_name if party_kind == "organization" else first_name and last_name
        same_role_confirmed = request.POST.get("same_role_confirmed") == "yes"
        # Keep all attempted values on every validation failure, in memory only.
        party.party_type = party_type
        party.party_type_name = party_type_names.get(party_type, "")
        party.organization_name = organization_name if party_kind == "organization" else ""
        for field in ("first_name", "middle_name", "last_name", "suffix"):
            setattr(party, field, request.POST.get(field, "").strip() if party_kind == "person" else "")
        for field, value in address.items():
            setattr(party, field, value)
        party.address_line_2 = address_line_2
        party.email = request.POST.get("email", "").strip()
        party.phone = request.POST.get("phone", "").strip()
        field_errors = validate_party(
            {field: getattr(party, field) for field in (*metadata["validation_rules"], "state")},
            metadata,
            organization=party_kind == "organization",
            address_started=address_started,
        )
        if not party_type or not has_name:
            messages.error(request, "Complete the party role and name.")
        elif filer_party_type and party_type == filer_party_type and not same_role_confirmed:
            same_role_error = True
        elif (address_requirement.required or address_started) and not address_complete:
            if address_requirement.required:
                messages.error(request, f"Complete the mailing address. {address_requirement.reason}")
            else:
                messages.error(request, "Complete the optional mailing address, or clear all of its fields.")
        elif not field_errors:
            # Choosing a court role by hand also establishes this party's side.
            party.party_side = side_for_party_type_name(party.party_type_name) or PartySide.OTHER
            party.save()

            return_to = return_target(request)
            remaining = [item for item in incomplete_parties(draft, party_types=party_types) if item.pk != party.pk]
            if remaining:
                url = reverse("party_details", kwargs={"jurisdiction": jurisdiction})
                return redirect(with_return_to(f"{url}?party={remaining[0].pk}", return_to))

            has_questions = bool(get_case_questions(draft)) or needs_amount_in_controversy(draft)
            draft.supplemental_fields = {
                **(draft.supplemental_fields or {}),
                "_case_questions_required": has_questions,
            }
            default_step = WorkflowStepKey.CASE_QUESTIONS if has_questions else WorkflowStepKey.PAYMENT
            draft.current_step = continue_step(draft, return_to, default_step)
            draft.save(update_fields=["supplemental_fields", "current_step", "updated_at"])
            return redirect(continue_url(draft, jurisdiction, return_to, default_step))

    address_requirement = party_address_requirement(draft, party, party_types=party_types)
    context = {
        **metadata,
        "field_errors": field_errors,
        "selected_state": party.state,
        "unsupported_state": bool(party.state) and party.state not in dict(metadata["state_choices"]),
        "is_logged_in": True,
        "filing_draft": draft_snapshot(draft),
        "party": party,
        # Someone already listed in the case has no use for "this party is me".
        "filer_is_party": filer_is_party(draft),
        "claimable": party_can_be_the_filer(party),
        "party_name": party_display_name(party),
        "filer_display_name": party_display_name(filer_row) if filer_row else "",
        # Claiming a row whose name is not the filer's replaces a person in
        # the case rather than confirming who they are, and the two are not
        # offered under the same words.
        "replaces_a_name": claim_replaces_a_name(filer_row, party),
        "primary_party_types": [
            item
            for item in party_types
            if item.get("required", False)
            or item["code"] == party.party_type
            or not any(role.get("required", False) for role in party_types)
        ],
        "other_party_types": [
            item
            for item in party_types
            if not item.get("required", False)
            and item["code"] != party.party_type
            and any(role.get("required", False) for role in party_types)
        ],
        "party_types": party_types,
        "party_kind": "organization" if party.organization_name else "person",
        "return_to": return_target(request),
        "court_code": draft.court_code,
        "address_required": address_requirement.required,
        "address_reason": address_requirement.reason,
        "show_optional_address": show_optional_address,
        "filer_party_type": filer_party_type,
        "filer_party_type_name": party_type_names.get(filer_party_type, filer_row.party_type_name if filer_row else ""),
        # Already given the filer's role and saved: that was confirmed then, or
        # came from the filer saying on Confirm case that this person is on
        # their side.
        "same_role_confirmed": bool(filer_party_type) and party.party_type == filer_party_type and not same_role_error,
        "same_role_error": same_role_error,
    }
    context.update(get_workflow_context(WorkflowStepKey.PARTY_DETAILS, jurisdiction, draft))
    return render(request, "efile/party_details.html", context)
