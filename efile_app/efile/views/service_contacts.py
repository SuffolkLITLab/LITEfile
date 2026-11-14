"""Edit recipients from the People page without sending documents."""

from django.contrib import messages
from django.core import signing
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDraft
from efile.services.current_drafts import get_current_draft
from efile.services.drafts import ACTIVE_DRAFT_STATUSES
from efile.services.efsp_payload import PayloadValidationError
from efile.services.filing_availability import draft_unavailable_message, unavailable_response
from efile.services.service_contacts import (
    KEY,
    ServiceContactError,
    create_contact,
    save_contacts,
    saved_service,
    scope,
    service_options,
)


def service_context(request, draft):
    saved = saved_service(draft)
    context = {
        "service_saved": saved,
        "service_draft_id": draft.pk,
        "service_stale": bool(saved and saved.get("scope") != scope(draft)),
        "service_scope": signing.dumps([draft.pk, scope(draft)], salt="service-contacts"),
        "service_open": request.GET.get("service") == "1",
    }
    if context["service_open"]:
        try:
            contacts, types = service_options(draft, get_tyler_token(request, draft.jurisdiction))
            selected = {item["contact_id"] for item in saved.get("contacts", [])}
            context["service_contacts"] = [{**item, "selected": item["contact_id"] in selected} for item in contacts]
            context["service_types"] = types
        except (ServiceContactError, PayloadValidationError, ValueError) as error:
            context["service_error"] = str(error)
    return context


@require_http_methods(["POST"])
def update_service_contacts(request, jurisdiction) -> HttpResponse:
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def failure(message, status=400):
        if wants_json:
            return JsonResponse({"success": False, "error": message}, status=status)
        return HttpResponseBadRequest(message)

    token = get_tyler_token(request, jurisdiction)
    if not request.user.is_authenticated or not token:
        if wants_json:
            return failure("Sign in again before changing recipients.", status=401)
        return redirect("efile_login", jurisdiction=jurisdiction)
    draft = get_current_draft(request, jurisdiction=jurisdiction)
    if draft is None:
        return failure("Open your filing before changing recipients.")
    if message := draft_unavailable_message(draft):
        if wants_json:
            return failure(message, status=409)
        return unavailable_response(request, draft, message)
    destination = reverse("parties", args=[jurisdiction]) + f"?draft={draft.pk}&service=1"
    if request.POST.get("return_to") == "review":
        destination += "&return_to=review"
    try:
        expected = signing.loads(request.POST.get("service_scope", ""), salt="service-contacts", max_age=3600)
        if expected != [draft.pk, scope(draft)]:
            raise ServiceContactError("Your case details changed. Check the recipients again.")
        if draft.status not in ACTIVE_DRAFT_STATUSES or draft.deletion_pending:
            return failure("This filing can no longer be changed.")
        if request.POST.get("action") == "create_contact":
            contact_id = create_contact(draft, token, request.POST)
            if wants_json:
                return JsonResponse(
                    {
                        "success": True,
                        "contact": {
                            "contact_id": contact_id,
                            "name": request.POST.get("contact_first_name", "").strip()
                            + " "
                            + request.POST.get("contact_last_name", "").strip(),
                            "email": request.POST.get("contact_email", "").strip(),
                        },
                    }
                )
            messages.success(request, "Contact added. Select them below to include them.")
        else:
            data = request.POST.copy()
            if data.get("action") == "clear_contacts":
                data.setlist("service_contact", [])
            selection = save_contacts(draft, token, data, persist=False)
            with transaction.atomic():
                draft = FilingDraft.objects.select_for_update().get(
                    pk=draft.pk, user=request.user, status__in=ACTIVE_DRAFT_STATUSES, deletion_pending=False
                )
                if expected != [draft.pk, scope(draft)]:
                    raise ServiceContactError("Your case details changed. Check the recipients again.")
                draft.supplemental_fields = {**(draft.supplemental_fields or {}), KEY: selection}
                draft.save(update_fields=["supplemental_fields", "updated_at"])
            if wants_json:
                context = service_context(request, draft)
                context.update(jurisdiction=jurisdiction, return_to=request.POST.get("return_to", ""))
                return JsonResponse(
                    {
                        "success": True,
                        "summary_html": render_to_string(
                            "efile/components/service_contacts.html", context, request=request
                        ),
                    }
                )
            destination = destination.replace("&service=1", "")
            messages.success(request, "Email recipients saved. No documents have been sent yet.")
    except signing.BadSignature:
        if wants_json:
            return JsonResponse(
                {"success": False, "error": "Reload the People page before changing recipients."}, status=400
            )
        messages.error(request, "Reload the People page before changing recipients.")
    except FilingDraft.DoesNotExist:
        return failure("This filing can no longer be changed.")
    except (ServiceContactError, PayloadValidationError, ValueError) as error:
        if wants_json:
            return JsonResponse({"success": False, "error": str(error)}, status=400)
        messages.error(request, str(error))
    return redirect(destination + "#email-recipients")
