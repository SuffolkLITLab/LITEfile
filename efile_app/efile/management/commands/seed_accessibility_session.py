"""Create deterministic, local-only data for browser accessibility checks."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.test import Client
from django.utils import timezone

from efile.models import FilingDocument, FilingDraft, FilingParty, FilingPlan
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.services.fee_quotes import record_fee_quote
from efile.workflow import ExistingCase, WorkflowStepKey


class Command(BaseCommand):
    help = "Seed a local browser session for the Axe accessibility suite."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Path for Playwright storage state JSON.")
        parser.add_argument("--origin", default="http://127.0.0.1:8000", help="Origin served to Playwright.")
        parser.add_argument(
            "--appeal",
            action="store_true",
            help="Seed a new-appeal draft for appellate browser checks.",
        )
        parser.add_argument(
            "--jurisdiction",
            choices=("illinois", "massachusetts"),
            default="illinois",
            help="Jurisdiction for the browser fixture (default: illinois).",
        )

    def handle(self, *args, **options):
        user_model = settings.AUTH_USER_MODEL
        from django.apps import apps

        user_class = apps.get_model(user_model)
        jurisdiction = options["jurisdiction"]
        is_illinois = jurisdiction == "illinois"
        username = "accessibility-checker" if is_illinois else f"accessibility-checker-{jurisdiction}"
        email = (
            "accessibility-checker@example.com" if is_illinois else f"accessibility-checker-{jurisdiction}@example.com"
        )
        user, _ = user_class.objects.update_or_create(
            username=username,
            defaults={
                "email": email,
                "tyler_jurisdiction": jurisdiction,
                "tyler_username": email,
                "first_name": "Avery",
                "last_name": "Checker",
            },
        )
        user.set_unusable_password()
        user.save()

        FilingDraft.objects.filter(user=user).delete()
        FilingPlan.objects.filter(user=user).delete()
        plan = FilingPlan.objects.create(user=user, jurisdiction=jurisdiction, title="Accessibility test filing")
        appeal = options["appeal"]
        if appeal and jurisdiction not in {"illinois", "massachusetts"}:
            raise ValueError("The appeal browser fixture supports Illinois and Massachusetts.")
        court_code = ("TAC1" if is_illinois else "appeals:acp") if appeal else "cook:cvd1"
        court_name = (
            ("Appellate Court - 1st District" if is_illinois else "Massachusetts Appeals Court (Panel)")
            if appeal
            else "Cook County"
        )
        case_category_code = ("21083" if is_illinois else "5796") if appeal else "6198"
        case_category_name = (
            ("Appeal" if is_illinois else "Appeals Court Panel Cases - Civil") if appeal else "Small Claims"
        )
        case_type_code = ("37653" if is_illinois else "7660") if appeal else "183541"
        case_type_name = ("Notice of Appeal - Civil" if is_illinois else "Contract dispute") if appeal else "Contract"
        filing_type_code = "30341" if appeal else "143132"
        filing_type_name = "Notice of Appeal" if appeal else "Complaint"
        draft = FilingDraft.objects.create(
            user=user,
            plan=plan,
            jurisdiction=jurisdiction,
            workflow_version=2,
            existing_case=ExistingCase.NEW,
            current_step=WorkflowStepKey.PARTIES if appeal else WorkflowStepKey.REVIEW,
            court_code=court_code,
            court_name=court_name,
            case_category_code=case_category_code,
            case_category_name=case_category_name,
            case_type_code=case_type_code,
            case_type_name=case_type_name,
            filing_type_code=filing_type_code,
            filing_type_name=filing_type_name,
            document_checklist_acknowledged=True,
            # Something for the confirm-filing screen to show, so its
            # document summary is rendered and audited too.
            extracted_guesses={"document title": "Complaint", "case title": "Checker v. Example"},
            selected_payment_account_id="a11y-payment-account",
            selected_payment_account_name="Accessibility payment account",
        )
        FilingDocument.objects.create(
            draft=draft,
            role=FilingDocument.Role.LEAD,
            name=f"{filing_type_name}.pdf" if appeal else "Accessibility complaint.pdf",
            original_filename=f"{filing_type_name}.pdf" if appeal else "Accessibility complaint.pdf",
            # This fixture starts downstream of preparation and confirmation.
            preparation="unchanged",
            preparation_reviewed_at=timezone.now(),
            filing_type_code=filing_type_code,
            filing_type_name=filing_type_name,
            document_type_code="public",
            document_type_name="Public",
        )
        FilingParty.objects.create(
            draft=draft,
            role="filer",
            sort_order=0,
            first_name="Avery",
            last_name="Checker",
            address_line_1="100 Main Street",
            city="Chicago" if is_illinois else "Boston",
            state="IL" if is_illinois else "MA",
            zip_code="60601" if is_illinois else "02108",
            email=user.email,
            party_type="plaintiff",
            party_type_name="Plaintiff",
            is_filing_party=True,
        )
        other_party = FilingParty.objects.create(
            draft=draft,
            role="other",
            sort_order=1,
            first_name="Jordan",
            last_name="Example",
            party_type="defendant" if appeal else "",
            party_type_name="Defendant" if appeal else "",
        )

        # A quote priced on this draft as it now stands, so Review shows it as
        # current rather than asking the court again.
        record_fee_quote(draft, "0.00", [])

        # Let Django's own test client construct the authenticated session. This
        # tracks framework changes to session-auth details without duplicating
        # private authentication keys in this browser-only fixture command.
        client = Client()
        client.force_login(user)
        session = client.session
        session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
        session["jurisdiction"] = jurisdiction
        session["auth_tokens"] = {f"TYLER-TOKEN-{jurisdiction.upper()}": "accessibility-test-token"}
        session.save()

        output = Path(options["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": settings.SESSION_COOKIE_NAME,
                            "value": session.session_key,
                            "domain": "127.0.0.1",
                            "path": "/",
                            "expires": -1,
                            "httpOnly": True,
                            "secure": False,
                            "sameSite": "Lax",
                        }
                    ],
                    "origins": [],
                },
                indent=2,
            )
        )
        self.stdout.write(self.style.SUCCESS(f"Seeded accessibility session for party {other_party.pk}."))
