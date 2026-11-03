"""TOTP-protected administration with jurisdiction-scoped purpose-built views."""

import csv
import re
from datetime import date, timedelta
from typing import cast

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.contrib.sessions.models import Session
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.html import format_html
from django_otp.admin import OTPAdminSite
from django_otp.plugins.otp_totp.models import TOTPDevice

from efile.models import FilingCodeJob, FilingDraft, PrivacyRequest, StaffRoleGrant, UserProfile
from efile.services.analytics import collection_health, report_rows
from efile.services.privacy import account_sessions, audit, create_request, preview, process_request, verify_request
from efile.staff_security import StaffLoginForm, jurisdictions_for, require_scope


class LookupForm(forms.Form):
    jurisdiction = forms.ChoiceField()
    account_id = forms.IntegerField(required=False, min_value=1)
    email = forms.EmailField(required=False)
    draft_id = forms.IntegerField(required=False, min_value=1)
    status = forms.ChoiceField(required=False, choices=[("", "Any status"), *FilingDraft.Status.choices])
    since = forms.DateField(required=False)
    until = forms.DateField(required=False)


class RequestForm(forms.Form):
    account_wide = forms.BooleanField(
        required=False,
        label="Delete this account and all of its LITEfile data",
        help_text="Includes this local account's filings, plans, browser sessions, and uploaded files.",
    )
    drafts = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Or select individual filings",
        help_text="Keep the account and remove only these filings. These choices are ignored when deleting the whole account.",
    )


class ReportForm(forms.Form):
    jurisdiction = forms.ChoiceField()
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    grouping = forms.ChoiceField(choices=[("month", "Monthly"), ("day", "Daily")])
    dimension = forms.ChoiceField(
        choices=[
            ("all", "Total"),
            ("case_type", "Case type"),
            ("filer_side", "Side of the case"),
            ("filing_for", "Filing for"),
            ("zip_code", "Filer ZIP code"),
            ("preparation", "Document preparation"),
            ("original_pdf_state", "Original PDF form fields"),
            ("usage_frequency", "Use frequency"),
            ("filing_type", "Filing code"),
            ("court", "Court division or location"),
            ("has_attorney", "Attorney representation"),
            ("fee_waiver", "Fee waiver submitted"),
            ("interview_source", "Guided interview source"),
            ("workflow_step", "Workflow step"),
            ("rejection_reason", "Court return reason"),
        ]
    )

    def clean(self):
        data = super().clean()
        if "start" in data and "end" in data:
            if data["start"] > data["end"] or (data["end"] - data["start"]).days > 3660:
                raise forms.ValidationError("Choose an ordered range of no more than ten years.")
        return data


class StaffSite(OTPAdminSite):
    login_form = StaffLoginForm
    site_header = "LITEfile staff"
    site_title = "LITEfile staff"
    index_title = "Staff tools"
    index_template = "efile/staff/index.html"
    site_url = None

    def has_permission(self, request):
        if not super().has_permission(request) or not isinstance(request.user.otp_device, TOTPDevice):
            return False
        if not request.user.otp_device.confirmed:
            return False
        verified = request.session.get("staff_verified_at", 0)
        if timezone.now().timestamp() - verified > settings.LITEFILE_STAFF_SESSION_SECONDS:
            return False
        if request.session.get("_auth_user_backend") != "django.contrib.auth.backends.ModelBackend":
            return False
        return bool(request.user.is_superuser or request.user.staff_roles.exists())

    def each_context(self, request):
        context = super().each_context(request)
        context.update(
            {
                "account_access": bool(jurisdictions_for(request.user, "accounts")),
                "analytics_access": bool(jurisdictions_for(request.user, "analytics")),
                "filing_codes_access": request.user.is_superuser,
            }
        )
        return context

    def get_urls(self):
        extra = [
            path("accounts/", self.admin_view(self.accounts), name="accounts"),
            path("accounts/<int:account_id>/", self.admin_view(self.account), name="account"),
            path("requests/", self.admin_view(self.requests), name="requests"),
            path("requests/<int:request_id>/", self.admin_view(self.privacy_request), name="privacy_request"),
            path("analytics/", self.admin_view(self.analytics), name="analytics"),
            path("authenticator/<int:account_id>/", self.admin_view(self.authenticator), name="authenticator"),
            path("filing-codes/", self.admin_view(self.filing_codes), name="filing_codes"),
        ]
        return extra + super().get_urls()

    def page(self, request, template, **context):
        return render(request, f"efile/staff/{template}.html", {**self.each_context(request), **context})

    def accounts(self, request):
        scopes = jurisdictions_for(request.user, "accounts")
        if not scopes:
            raise PermissionDenied
        form = LookupForm(request.POST if request.method == "POST" else request.session.get("staff_lookup"))
        cast(forms.ChoiceField, form.fields["jurisdiction"]).choices = [(scope, scope.capitalize()) for scope in scopes]
        accounts = UserProfile.objects.none()
        if form.is_valid():
            data = form.cleaned_data
            require_scope(request.user, data["jurisdiction"], "accounts")
            if request.method == "POST":
                request.session["staff_lookup"] = {
                    key: str(value) if isinstance(value, date) else value for key, value in data.items()
                }
                audit(request.user, data["jurisdiction"], "account_lookup", "viewed")
            accounts = UserProfile.objects.filter(
                is_staff=False, is_superuser=False, tyler_jurisdiction=data["jurisdiction"]
            ).order_by("pk")
            if data["account_id"]:
                accounts = accounts.filter(pk=data["account_id"])
            if data["email"]:
                accounts = accounts.filter(Q(email__iexact=data["email"]) | Q(tyler_username__iexact=data["email"]))
            # Filter by one matching draft rather than independent joins.
            if any(data[key] for key in ("draft_id", "status", "since", "until")):
                drafts = FilingDraft.objects.filter(jurisdiction=data["jurisdiction"])
                if data["draft_id"]:
                    drafts = drafts.filter(pk=data["draft_id"])
                if data["status"]:
                    drafts = drafts.filter(status=data["status"])
                if data["since"]:
                    drafts = drafts.filter(created_at__date__gte=data["since"])
                if data["until"]:
                    drafts = drafts.filter(created_at__date__lte=data["until"])
                accounts = accounts.filter(pk__in=drafts.values("user_id"))
        return self.page(
            request,
            "accounts",
            title="Find an account",
            form=form,
            accounts=Paginator(accounts, 25).get_page(request.GET.get("page")),
        )

    def account(self, request, account_id):
        target = get_object_or_404(UserProfile, pk=account_id, is_staff=False, is_superuser=False)
        require_scope(request.user, target.tyler_jurisdiction, "accounts")
        drafts = target.filing_drafts.filter(jurisdiction=target.tyler_jurisdiction).order_by("-created_at")
        form = RequestForm(
            request.POST if request.method == "POST" and request.POST.get("action") == "request" else None
        )
        cast(forms.MultipleChoiceField, form.fields["drafts"]).choices = [
            (str(draft.pk), f"Draft #{draft.pk}: {draft.status}, {draft.created_at:%Y-%m-%d}") for draft in drafts
        ]
        sessions = account_sessions(target)
        session_rows = [
            {
                "token": salted_hmac("staff-session", session.pk).hexdigest(),
                "expires": session.expire_date,
                "estimated_activity": session.expire_date - timedelta(seconds=settings.SESSION_COOKIE_AGE),
            }
            for session in sessions
        ]
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "revoke":
                token = request.POST.get("session", "")
                ids = [
                    session.pk for session in sessions if salted_hmac("staff-session", session.pk).hexdigest() == token
                ]
                Session.objects.filter(pk__in=ids).delete()
                audit(
                    request.user,
                    target.tyler_jurisdiction,
                    "session_revoked",
                    "completed",
                    counts={"sessions": len(ids)},
                )
                return redirect("litefile_staff:account", account_id=target.pk)
            if action == "request" and form.is_valid():
                try:
                    item = create_request(
                        request.user,
                        target,
                        account_wide=form.cleaned_data["account_wide"],
                        draft_ids=[int(value) for value in form.cleaned_data["drafts"]],
                    )
                except ValueError as exc:
                    form.add_error(None, str(exc))
                else:
                    return redirect("litefile_staff:privacy_request", request_id=item.pk)
        audit(request.user, target.tyler_jurisdiction, "account_viewed", "viewed")
        return self.page(
            request,
            "account",
            title="Account details",
            target=target,
            drafts=Paginator(drafts, 25).get_page(request.GET.get("page")),
            sessions=session_rows,
            form=form,
            plans=target.filing_plans.count(),
            archived=target.archived_cases.count(),
            deletion_enabled=settings.LITEFILE_PRIVACY_DELETION_ENABLED,
            open_requests=target.privacy_requests.filter(jurisdiction=target.tyler_jurisdiction)
            .exclude(status=PrivacyRequest.Status.COMPLETED)
            .order_by("-created_at"),
        )

    def requests(self, request):
        scopes = jurisdictions_for(request.user, "accounts")
        if not scopes:
            raise PermissionDenied
        items = PrivacyRequest.objects.filter(jurisdiction__in=scopes).order_by("-created_at")
        return self.page(
            request, "requests", title="Deletion requests", items=Paginator(items, 25).get_page(request.GET.get("page"))
        )

    def privacy_request(self, request, request_id):
        item = get_object_or_404(PrivacyRequest, pk=request_id)
        require_scope(request.user, item.jurisdiction, "accounts")
        if request.method == "POST":
            try:
                action = request.POST.get("action")
                if action == "verify" and request.POST.get("verified") == "yes":
                    verify_request(item.pk, request.user)
                elif action == "process" and request.POST.get("confirmed") == str(item.reference):
                    try:
                        expected = signing.loads(
                            request.POST.get("preview", ""),
                            salt="staff-deletion-preview",
                            max_age=settings.LITEFILE_STAFF_SESSION_SECONDS,
                        )
                    except signing.BadSignature as exc:
                        raise ValueError("Review a fresh deletion preview before confirming.") from exc
                    if expected["request"] != item.pk or expected["updated_at"] != item.updated_at.isoformat():
                        raise ValueError("Review a fresh deletion preview before confirming.")
                    process_request(item.pk, request.user, expected=expected)
                    # This request's cached session must not restore a cleared
                    # search locator when SessionMiddleware saves the response.
                    request.session.pop("staff_lookup", None)
                elif (
                    action == "external_resolved"
                    and item.status == "completed"
                    and request.POST.get("resolved") == "yes"
                ):
                    item.external_cleanup_pending = False
                    item.save(update_fields=["external_cleanup_pending", "updated_at"])
                    audit(
                        request.user,
                        item.jurisdiction,
                        "external_cleanup",
                        "operator_confirmed",
                        reference=item.reference,
                    )
                else:
                    raise ValueError("Explicit verification or confirmation is required.")
            except ValueError as exc:
                messages.error(request, str(exc))
            return redirect("litefile_staff:privacy_request", request_id=item.pk)
        plan = preview(item) if item.target_id else {"counts": item.counts, "blockers": []}
        token = signing.dumps(
            {
                "request": item.pk,
                "updated_at": item.updated_at.isoformat(),
                "counts": plan["counts"],
                "draft_ids": plan.get("draft_ids", []),
            },
            salt="staff-deletion-preview",
        )
        return self.page(
            request,
            "privacy_request",
            title="Deletion request",
            item=item,
            plan=plan,
            deletion_enabled=settings.LITEFILE_PRIVACY_DELETION_ENABLED,
            preview_token=token,
        )

    def analytics(self, request):
        scopes = jurisdictions_for(request.user, "analytics")
        if not scopes:
            raise PermissionDenied
        last_month = timezone.now().date().replace(day=1) - timedelta(days=1)
        form = ReportForm(
            request.GET or None,
            initial={"start": last_month.replace(day=1), "end": last_month, "grouping": "month", "dimension": "all"},
        )
        cast(forms.ChoiceField, form.fields["jurisdiction"]).choices = [(scope, scope.capitalize()) for scope in scopes]
        rows = []
        if form.is_valid():
            data = form.cleaned_data
            require_scope(request.user, data["jurisdiction"], "analytics")
            try:
                rows = report_rows(
                    [data["jurisdiction"]],
                    data["start"],
                    data["end"],
                    grouping=data["grouping"],
                    dimension=data["dimension"],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            if request.GET.get("format") == "csv" and not form.errors:
                if not settings.LITEFILE_ANALYTICS_EXPORT_ENABLED:
                    raise PermissionDenied
                response = HttpResponse(content_type="text/csv")
                response["Content-Disposition"] = 'attachment; filename="litefile-usage.csv"'
                writer = csv.DictWriter(
                    response,
                    fieldnames=[
                        "period",
                        "jurisdiction",
                        "metric",
                        "filing_kind",
                        "dimension",
                        "value",
                        "count",
                        "contributors",
                    ],
                )
                writer.writeheader()
                for row in rows:
                    # Guard spreadsheet formula interpretation even for config labels.
                    writer.writerow(
                        {
                            key: "'" + value if isinstance(value, str) and re.match(r"^[=+@-]", value) else value
                            for key, value in row.items()
                        }
                    )
                audit(request.user, data["jurisdiction"], "analytics_export", "exported", counts={"rows": len(rows)})
                return response
        return self.page(
            request,
            "analytics",
            title="Aggregate usage",
            form=form,
            rows=rows,
            health=collection_health(scopes),
            collecting=settings.LITEFILE_ANALYTICS_ENABLED,
            export_enabled=settings.LITEFILE_ANALYTICS_EXPORT_ENABLED,
        )

    def filing_codes(self, request):
        """Resync codes from the EFSP, or rebuild search indexes; the code index worker runs them."""
        if not request.user.is_superuser:
            raise PermissionDenied
        from efile.services.filing_code_copy import status
        from efile.services.filing_code_search import rules

        jurisdictions = rules()[0]["jurisdictions"]
        if request.method == "POST":
            kind = request.POST.get("kind", "")
            jurisdiction = request.POST.get("jurisdiction", "")
            if kind not in FilingCodeJob.Kind.values or jurisdiction not in ["", *jurisdictions]:
                messages.error(request, "Choose a valid action and jurisdiction.")
            elif FilingCodeJob.objects.filter(
                kind=kind, jurisdiction=jurisdiction, status=FilingCodeJob.Status.QUEUED
            ).exists():
                messages.info(request, "That is already queued.")
            else:
                FilingCodeJob.objects.create(kind=kind, jurisdiction=jurisdiction, requested_by=request.user)
                audit(request.user, jurisdiction, f"filing_codes_{kind}", "queued")
                messages.success(
                    request,
                    f"{FilingCodeJob.Kind(kind).label} queued for {jurisdiction.capitalize() or 'every jurisdiction'}. "
                    "The code index worker starts it within a minute.",
                )
            return redirect("litefile_staff:filing_codes")
        return self.page(
            request,
            "filing_codes",
            title="Filing codes",
            jurisdictions=jurisdictions,
            statuses=[status(jurisdiction) for jurisdiction in jurisdictions],
            jobs=FilingCodeJob.objects.select_related("requested_by")[:15],
            schedule=f"{settings.FILING_CODE_SYNC_TIME} {settings.FILING_CODE_SYNC_TIMEZONE}",
            source=settings.FILING_CODE_SYNC_MODE,
        )

    def authenticator(self, request, account_id):
        if not request.user.is_superuser:
            raise PermissionDenied
        target = get_object_or_404(UserProfile, pk=account_id, is_staff=True, is_active=True)
        config_url = ""
        if request.method == "POST" and request.POST.get("confirmed") == "yes":
            from django.db import transaction

            with transaction.atomic():
                UserProfile.objects.select_for_update().get(pk=target.pk)
                if TOTPDevice.objects.filter(user=target).exists():
                    messages.error(
                        request, "An authenticator already exists. Use the console recovery procedure to reset it."
                    )
                else:
                    device = TOTPDevice.objects.create(user=target, name="Staff authenticator", confirmed=True)
                    config_url = device.config_url
                    audit(request.user, "", "totp_provisioned", "completed")
        return self.page(
            request, "authenticator", title="Provision an authenticator", target=target, config_url=config_url
        )


staff_site = StaffSite(name="litefile_staff")


class SuperuserOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    has_add_permission = has_view_permission
    has_change_permission = has_view_permission
    has_delete_permission = has_view_permission

    def log_addition(self, request, obj, message):
        audit(request.user, "", "staff_configuration", "added")

    def log_change(self, request, obj, message):
        audit(request.user, "", "staff_configuration", "changed")

    def log_deletions(self, request, queryset):
        audit(request.user, "", "staff_configuration", "deleted", counts={"records": queryset.count()})


class StaffUserAdmin(SuperuserOnlyAdmin, UserAdmin):
    list_display = ("username", "is_active", "is_superuser")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Staff access", {"fields": ("is_active", "is_superuser", "authenticator")}),
    )
    readonly_fields = ("authenticator",)
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("username", "password1", "password2")}),)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_staff=True)

    def save_model(self, request, obj, form, change):
        obj.is_staff = True
        super().save_model(request, obj, form, change)

    @admin.display(description="TOTP setup")
    def authenticator(self, obj):
        return format_html(
            '<a href="{}">Provision an authenticator</a>', reverse("litefile_staff:authenticator", args=[obj.pk])
        )


class RoleAdmin(SuperuserOnlyAdmin):
    list_display = ("user", "jurisdiction", "role")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            kwargs["queryset"] = UserProfile.objects.filter(is_staff=True, is_active=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


staff_site.register(UserProfile, StaffUserAdmin)
staff_site.register(StaffRoleGrant, RoleAdmin)
