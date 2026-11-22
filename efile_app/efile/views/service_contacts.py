"""Explain service choices and edit electronic-service recipients."""

from django.contrib import messages
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingDraft
from efile.services.account_profile import cached_account_profile
from efile.services.current_drafts import ensure_current_draft, get_current_draft
from efile.services.drafts import ACTIVE_DRAFT_STATUSES, draft_snapshot
from efile.services.efsp_payload import PayloadValidationError
from efile.services.filing_availability import draft_unavailable_message, unavailable_response
from efile.services.service_contacts import (
    KEY,
    ServiceContactAuthenticationError,
    ServiceContactError,
    create_contact,
    register_self_service_contact,
    save_contacts,
    saved_service,
    scope,
    service_options,
    submitted_case_id,
)
from efile.utils.config_loader import config_loader
from efile.utils.jurisdiction_stuff import has_jurisdiction_login, redirect_to_jurisdiction_login
from efile.workflow import WorkflowStepKey, continue_step, continue_url, get_workflow_context, return_target


def service_context(request, draft):
    saved = saved_service(draft)
    context = {
        "service_saved": saved,
        "service_initial": draft.existing_case != "existing",
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
    destination = reverse("service", args=[jurisdiction]) + f"?draft={draft.pk}&service=1"
    if return_target(request):
        destination += "&return_to=" + return_target(request)
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
                draft.supplemental_fields = {
                    **(draft.supplemental_fields or {}),
                    KEY: selection,
                    "_service_method": {"method": "electronic" if selection else "", "scope": scope(draft)},
                }
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
            messages.success(request, "Service recipients saved. No documents have been sent yet.")
    except signing.BadSignature:
        if wants_json:
            return JsonResponse(
                {"success": False, "error": "Reload the Service page before changing recipients."}, status=400
            )
        messages.error(request, "Reload the Service page before changing recipients.")
    except FilingDraft.DoesNotExist:
        return failure("This filing can no longer be changed.")
    except (ServiceContactError, PayloadValidationError, ValueError) as error:
        if wants_json:
            return JsonResponse({"success": False, "error": str(error)}, status=400)
        messages.error(request, str(error))
    return redirect(destination + "#email-recipients")


@require_http_methods(["GET", "POST"])
def service(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)
    draft = ensure_current_draft(request, jurisdiction, current_step=WorkflowStepKey.SERVICE, workflow_version=2)
    if message := draft_unavailable_message(draft):
        return unavailable_response(request, draft, message)
    if not draft.court_code or not draft.case_type_code:
        return redirect("extraction_review", jurisdiction=jurisdiction)
    return_to = return_target(request)
    saved = saved_service(draft)
    choice = (draft.supplemental_fields or {}).get("_service_method", {})
    method = (
        "electronic"
        if saved.get("contacts") or request.GET.get("service") == "1"
        else (choice.get("method", "") if choice.get("scope") == scope(draft) else "")
    )
    error = ""
    if request.method == "POST":
        method = request.POST.get("service_method", "")
        try:
            expected = signing.loads(request.POST.get("service_scope", ""), salt="service-contacts", max_age=3600)
            if expected != [draft.pk, scope(draft)]:
                raise ServiceContactError("Your case details changed. Reload this page and check the service rules.")
            if method not in {"other", "electronic"}:
                raise ServiceContactError("Choose how you will handle service before continuing.")
            with transaction.atomic():
                draft = FilingDraft.objects.select_for_update().get(
                    pk=draft.pk, user=request.user, status__in=ACTIVE_DRAFT_STATUSES, deletion_pending=False
                )
                if expected != [draft.pk, scope(draft)]:
                    raise ServiceContactError(
                        "Your case details changed. Reload this page and check the service rules."
                    )
                current = saved_service(draft)
                if method == "electronic" and (
                    not current.get("contacts")
                    or current.get("scope") != scope(draft)
                    or not current.get("rules_checked")
                ):
                    raise ServiceContactError(
                        "Check the rules, then choose and save the people you can serve electronically."
                    )
                fields = dict(draft.supplemental_fields or {})
                if method == "other":
                    fields[KEY] = {}
                fields["_service_method"] = {"method": method, "scope": scope(draft)}
                draft.supplemental_fields = fields
                draft.current_step = continue_step(draft, return_to, WorkflowStepKey.PAYMENT)
                draft.save(update_fields=["supplemental_fields", "current_step", "updated_at"])
            return redirect(continue_url(draft, jurisdiction, return_to, WorkflowStepKey.PAYMENT))
        except signing.BadSignature:
            error = "Reload the Service page before continuing."
        except FilingDraft.DoesNotExist:
            return HttpResponseBadRequest("This filing can no longer be changed.")
        except ServiceContactError as exc:
            error = str(exc)
    context = service_context(request, draft)
    context.update(
        is_logged_in=True,
        filing_draft=draft_snapshot(draft),
        return_to=return_to,
        service_method=method,
        service_choice_error=error,
        service_initial=draft.existing_case != "existing",
    )
    context.update(get_workflow_context(WorkflowStepKey.SERVICE, jurisdiction, draft))
    return render(request, "efile/service.html", context)


@require_http_methods(["GET", "POST"])
def self_service_contact(request, public_id):
    draft = get_object_or_404(
        FilingDraft.objects.select_related("user"),
        public_id=public_id,
        status=FilingDraft.Status.SUBMITTED,
        deletion_pending=False,
    )
    if not has_jurisdiction_login(request, draft.jurisdiction):
        return redirect_to_jurisdiction_login(request, draft.jurisdiction)
    if draft.user_id != request.user.pk:
        raise Http404
    token = get_tyler_token(request, draft.jurisdiction)
    error = ""
    profile = (cached_account_profile(request, draft.jurisdiction) or {}) if request.method == "GET" else {}
    first = request.POST.get("first_name", profile.get("first_name") or request.user.first_name)
    last = request.POST.get("last_name", profile.get("last_name") or request.user.last_name)
    if request.method == "POST":
        try:
            if request.POST.get("consent") != "yes":
                raise ServiceContactError(
                    "Confirm that you want to receive future service at your account email address."
                )
            register_self_service_contact(draft, token, first, last)
            messages.success(
                request, _("You are registered for future service at %(email)s.") % {"email": request.user.email}
            )
            return redirect("self_service_contact", public_id=draft.public_id)
        except ServiceContactAuthenticationError:
            return redirect_to_jurisdiction_login(request, draft.jurisdiction)
        except (ServiceContactError, ValueError) as exc:
            error = str(exc)
    return render(
        request,
        "efile/self_service_contact.html",
        {
            "draft": draft,
            "jurisdiction": draft.jurisdiction,
            "is_logged_in": True,
            "config": config_loader.load_jurisdiction_config(draft.jurisdiction),
            "case_ready": bool(submitted_case_id(draft)),
            "registration_error": error,
            "first_name": first,
            "last_name": last,
            "account_email": request.user.email,
        },
    )
