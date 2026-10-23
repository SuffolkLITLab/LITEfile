"""Explore indexed filing paths while preserving every saved classification."""

from django.db import transaction

from efile.models import FilingDocument, FilingDraft
from efile.services.filing_code_search import FACETS, validate_path
from efile.services.handoff import HandoffError, apply_filing_type_flags, record
from efile.workflow import ExistingCase


def locked_choices(draft, document):
    return {facet: getattr(document if facet == "filing_type" else draft, f"{facet}_code") for facet in FACETS}


def lookup_offered(draft, document):
    """New cases only, and only while some court choice is still missing."""
    return draft.existing_case == ExistingCase.NEW and not all(locked_choices(draft, document).values())


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
        corrections = list(current.correction_fields)
        for facet in FACETS:
            obj = target if facet == "filing_type" else current
            field = f"{facet}_code"
            if getattr(obj, field):
                continue
            setattr(obj, field, str(validated[facet]["code"]))
            setattr(obj, f"{facet}_name", validated[facet]["name"])
            update_fields = [field, f"{facet}_name", "updated_at"]
            if obj is target:
                update_fields += apply_filing_type_flags(target, validated[facet])
            obj._metadata_kind = "code_search_replacement"
            try:
                obj.save(update_fields=update_fields)
            finally:
                del obj._metadata_kind
            event_path = f"documents.{target.pk}.{field}" if obj is target else field
            record(current, event_path, "user_confirmation", {**validated[facet], "source": "code search"})
            if event_path in corrections:
                corrections.remove(event_path)
        if corrections != current.correction_fields:
            FilingDraft.objects.filter(pk=current.pk).update(correction_fields=corrections)
