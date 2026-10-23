"""Explore indexed filing paths while preserving every saved classification."""

from django.db import transaction

from efile.models import FilingDocument, FilingDraft
from efile.services.filing_code_search import validate_path
from efile.services.handoff import HandoffError, record
from efile.workflow import ExistingCase

CASE_FIELDS = ("court", "case_category", "case_type")


def locked_choices(draft, document):
    return {
        **{facet: getattr(draft, f"{facet}_code") for facet in CASE_FIELDS},
        "filing_type": document.filing_type_code,
    }


def compatible(draft, document, path):
    return path["initial"] == (draft.existing_case == ExistingCase.NEW) and all(
        not code or str(path[facet]["code"]) == code for facet, code in locked_choices(draft, document).items()
    )


def apply_replacement(draft, document, path):
    """Validate live first, then recheck locked values before filling only blanks."""
    if draft.existing_case != ExistingCase.NEW:
        raise HandoffError("Use the confirmed case’s filing choices for an existing case.", status=409)
    validated = validate_path(path)
    with transaction.atomic():
        try:
            current = FilingDraft.objects.select_for_update().get(pk=draft.pk, user=draft.user)
            target = current.documents.select_for_update().get(pk=document.pk)
        except (FilingDraft.DoesNotExist, FilingDocument.DoesNotExist) as exc:
            raise HandoffError(
                "Your documents changed. Return to your saved filing and try again.", status=409
            ) from exc
        if current.status != FilingDraft.Status.DRAFT or current.deletion_pending:
            raise HandoffError("This draft is no longer editable.", status=409)
        if (
            current.existing_case != ExistingCase.NEW
            or locked_choices(current, target) != locked_choices(draft, document)
            or not compatible(current, target, validated)
        ):
            raise HandoffError("Your saved choices changed. Search again; nothing was replaced.", status=409)
        changed = False
        for facet in (*CASE_FIELDS, "filing_type"):
            obj = target if facet == "filing_type" else current
            field = f"{facet}_code"
            if getattr(obj, field):
                continue
            setattr(obj, field, str(validated[facet]["code"]))
            setattr(obj, f"{facet}_name", validated[facet]["name"])
            update_fields = [field, f"{facet}_name", "updated_at"]
            if obj is target:
                target.filing_requires_amount_in_controversy = (
                    str(validated[facet].get("amountincontroversy", "")).lower() == "required"
                )
                update_fields.append("filing_requires_amount_in_controversy")
                if not target.document_type_code:
                    target.document_type_confirmed = False
                    update_fields.append("document_type_confirmed")
            obj._metadata_kind = "code_search_replacement"
            try:
                obj.save(update_fields=update_fields)
            finally:
                del obj._metadata_kind
            event_path = f"documents.{target.pk}.{field}" if obj is target else field
            record(current, event_path, "user_confirmation", {**validated[facet], "source": "code search"})
            if event_path in current.correction_fields:
                current.correction_fields.remove(event_path)
                FilingDraft.objects.filter(pk=current.pk).update(correction_fields=current.correction_fields)
            changed = True
        return changed
