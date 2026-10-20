"""Operations on the durable filing draft aggregate.

This module deliberately has no dependency on HTTP requests or sessions. Request/
session selection lives in ``current_drafts``; the wire (de)serialization between
the browser's flat case_data/upload_data blobs and the typed model lives here.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import QuerySet

from efile.models import FilingDocument, FilingDraft, FilingParty
from efile.services.document_preparation import cleanup_unreferenced_uploads
from efile.workflow import WorkflowStepKey, legacy_existing_case_value, normalize_existing_case

ACTIVE_DRAFT_STATUSES = (FilingDraft.Status.DRAFT, FilingDraft.Status.ERROR)
# A draft mid-submission is still the user's current draft (so the submit flow can
# read its own data), but it is not offered for resume in listings.
CURRENT_DRAFT_STATUSES = (*ACTIVE_DRAFT_STATUSES, FilingDraft.Status.SUBMITTING)


def active_drafts_for(user, *, jurisdiction: str | None = None) -> QuerySet[FilingDraft]:
    """Return active drafts owned by ``user``, newest first."""

    drafts = FilingDraft.objects.filter(user=user, status__in=ACTIVE_DRAFT_STATUSES, deletion_pending=False)
    if jurisdiction is not None:
        drafts = drafts.filter(jurisdiction=jurisdiction)
    return drafts.order_by("-updated_at")


def get_active_draft(
    *,
    user,
    draft_id: int | str | None = None,
    jurisdiction: str | None = None,
    statuses: tuple[str, ...] = ACTIVE_DRAFT_STATUSES,
) -> FilingDraft | None:
    """Get an owned draft by ID, or the user's most recent one, within ``statuses``."""

    drafts = FilingDraft.objects.filter(user=user, status__in=statuses, deletion_pending=False)
    if jurisdiction is not None:
        drafts = drafts.filter(jurisdiction=jurisdiction)
    drafts = drafts.order_by("-updated_at")
    if draft_id is not None:
        return drafts.filter(pk=draft_id).first()
    return drafts.first()


@transaction.atomic
def create_draft(
    *,
    user,
    jurisdiction: str,
    current_step: WorkflowStepKey | str = WorkflowStepKey.OPTIONS,
    workflow_version: int = 2,
) -> FilingDraft:
    """Create a durable draft owned by an authenticated user."""

    if not getattr(user, "is_authenticated", False):
        raise ValueError("A filing draft must have an authenticated owner")
    if not jurisdiction:
        raise ValueError("A filing draft must have a jurisdiction")

    from efile.models import PrivacyRequest, UserProfile

    UserProfile.objects.select_for_update().get(pk=user.pk)
    if (
        user.is_staff
        or PrivacyRequest.objects.filter(target=user, status="processing").exists()
        or user.filing_drafts.filter(deletion_pending=True).exists()
    ):
        raise ValueError("This account is unavailable for filing.")

    return FilingDraft.objects.create(
        user=user,
        jurisdiction=jurisdiction,
        current_step=str(current_step),
        workflow_version=workflow_version,
        # A filer who asked us to remember "no AI" starts every filing that way.
        ai_assistance_opted_out=bool(getattr(user, "ai_assistance_opted_out", False)),
    )


def set_current_step(draft: FilingDraft, current_step: WorkflowStepKey | str) -> FilingDraft:
    """Advance or rewind a draft's current UI step when it changed."""

    step = str(current_step)
    if draft.current_step != step:
        draft.current_step = step
        draft.save(update_fields=["current_step", "updated_at"])
    return draft


# --- Wire (de)serialization -------------------------------------------------
#
# The browser speaks a flat "case_data" / "upload_data" JSON blob. These maps are
# the *entire* contract: any key not listed here is deliberately not persisted.
# Adding a field a screen needs to keep means adding a typed column/party field
# and an entry here -- there is no catch-all blob.

_MISSING = object()

# Draft scalar column -> the wire keys it may arrive under (first present wins).
_DRAFT_FIELD_SOURCES: dict[str, tuple[str, ...]] = {
    "court_code": ("court", "court_code"),
    "court_name": ("court_name",),
    # The dropdown flow sends the bare name ("case_category"); the existing-case
    # lookup sends the explicit "*_code" form. Accept both.
    "case_category_code": ("case_category", "case_category_code"),
    "case_category_name": ("case_category_name",),
    "case_type_code": ("case_type", "case_type_code"),
    "case_type_name": ("case_type_name",),
    "case_subtype_code": ("case_subtype", "case_subtype_code"),
    "case_subtype_name": ("case_subtype_name",),
    "filing_type_code": ("filing_type", "filing_type_id"),
    "filing_type_name": ("filing_type_name",),
    "document_type_code": ("document_type",),
    "document_type_name": ("document_type_name",),
    "existing_case": ("existing_case",),
    "previous_case_id": ("case_tracking_id", "previous_case_id"),
    "docket_number": ("case_docket_id", "case_number", "docket_number"),
    "case_title": ("case_title", "case_title_text"),
    "selected_payment_account_id": ("selected_payment_account", "payment_account_id"),
    "selected_payment_account_name": ("selected_payment_account_name",),
    "name_change_reason": ("reason_for_name_change", "reason_for_change"),
}

# FilingParty role -> {model field: wire keys}. Petitioner party_type is special
# (three legacy aliases for one value) and handled separately. Respondent and
# defendant are aliases because filing types use those terms interchangeably.
_PARTY_SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "petitioner": {
        "first_name": ("petitioner_first_name",),
        "last_name": ("petitioner_last_name",),
        "address_line_1": ("petitioner_address",),
        "email": ("petitioner_email",),
        "phone": ("petitioner_phone",),
    },
    "new_name": {
        "first_name": ("new_first_name",),
        "middle_name": ("new_middle_name",),
        "last_name": ("new_last_name",),
        "suffix": ("new_suffix",),
        "party_type": ("new_name_party_type",),
    },
    "respondent": {
        "first_name": ("respondent_first_name", "defendant_first_name"),
        "middle_name": ("respondent_middle_name", "defendant_middle_name"),
        "last_name": ("respondent_last_name", "defendant_last_name"),
        "suffix": ("respondent_suffix", "defendant_suffix"),
        "party_type": ("respondent_name_party_type", "respondent_party_type", "defendant_party_type"),
    },
    "other": {
        "first_name": ("other_first_name",),
        "last_name": ("other_last_name",),
        "party_type": ("other_party_type",),
        "address_line_1": ("other_address_line_1",),
        "address_line_2": ("other_address_line_2",),
        "city": ("other_address_city",),
        "state": ("other_address_state",),
        "zip_code": ("other_address_zip",),
        "email": ("other_email",),
        "phone": ("other_phone_number",),
    },
}

_PETITIONER_PARTY_TYPE_KEYS = ("determined_party_type", "party_type", "petitioner_party_type")

# Area-of-law questionnaire fields that persist in ``supplemental_fields`` instead
# of a dedicated column. Keep this allowlist aligned with fields currently rendered
# from the state configuration. Values are stored as received rather than being
# string-coerced like the typed columns.
_SUPPLEMENTAL_FIELDS = ("has_children", "child_count")


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return _MISSING


def _as_str(value: Any) -> str:
    return "" if value in (None, "") else str(value)


def _put(data: dict[str, Any], key: str, value: Any) -> None:
    """Add ``key`` to the wire blob only when it carries a real value."""

    if value not in (None, "", [], {}):
        data[key] = value


@transaction.atomic
def write_case_data(
    draft: FilingDraft,
    case_data: dict[str, Any] | None,
    *,
    current_step: WorkflowStepKey | str | None = None,
) -> FilingDraft:
    """Persist a (possibly partial) case_data blob onto the draft.

    Only keys present in ``case_data`` are written; omitted fields are left as-is
    so per-screen partial saves don't clobber earlier steps.
    """

    data = dict(case_data or {})
    draft.refresh_from_db(from_queryset=FilingDraft.objects.select_for_update())
    old_identity = (draft.court_code, draft.previous_case_id, draft.existing_case)
    update_fields: list[str] = []

    for field, sources in _DRAFT_FIELD_SOURCES.items():
        value = _first_present(data, sources)
        if value is _MISSING:
            continue
        value = _as_str(value)
        if field == "existing_case":
            value = normalize_existing_case(value)
        if getattr(draft, field) != value:
            setattr(draft, field, value)
            update_fields.append(field)

    # existing_cases imports this module's draft statuses.
    from efile.services.existing_cases import SNAPSHOT_CASE_FIELDS, clear_case_import

    if old_identity != (draft.court_code, draft.previous_case_id, draft.existing_case):
        clear_case_import(draft)

    elif draft.existing_case_snapshot.get("status") == "loaded":
        snapshot = draft.existing_case_snapshot
        for field in SNAPSHOT_CASE_FIELDS:
            if getattr(draft, field) != snapshot[field]:
                setattr(draft, field, snapshot[field])
                update_fields.append(field)

    if "optional_services" in data:
        services = data.get("optional_services") or []
        if draft.optional_services != services:
            draft.optional_services = services
            update_fields.append("optional_services")

    supplemental_updates = {key: data[key] for key in _SUPPLEMENTAL_FIELDS if key in data}
    if supplemental_updates:
        merged = {**draft.supplemental_fields, **supplemental_updates}
        if merged != draft.supplemental_fields:
            draft.supplemental_fields = merged
            update_fields.append("supplemental_fields")

    if current_step is not None and draft.current_step != str(current_step):
        draft.current_step = str(current_step)
        update_fields.append("current_step")

    # Legacy clients still send the primary type here. Write explicit edits
    # through to the authoritative lead before saving the summary.
    filing_fields = {
        field: _as_str(value)
        for field in ("filing_type_code", "filing_type_name")
        if (value := _first_present(data, _DRAFT_FIELD_SOURCES[field])) is not _MISSING
    }
    if filing_fields:
        lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
        if lead is not None:
            for field, value in filing_fields.items():
                setattr(lead, field, value)
            lead.save(update_fields=[*filing_fields, "updated_at"])

    if update_fields:
        draft.save(update_fields=sorted({*update_fields, "updated_at"}))

    _write_parties(draft, data)
    return draft


def _write_parties(draft: FilingDraft, data: dict[str, Any]) -> None:
    for role, spec in _PARTY_SPECS.items():
        values = {}
        for model_field, wire_keys in spec.items():
            value = _first_present(data, wire_keys)
            if value is not _MISSING:
                values[model_field] = _as_str(value)
        if role == "petitioner":
            party_type = _first_present(data, _PETITIONER_PARTY_TYPE_KEYS)
            if party_type is not _MISSING:
                values["party_type"] = _as_str(party_type)
        if values and not draft.existing_case_snapshot:
            FilingParty.objects.update_or_create(draft=draft, role=role, sort_order=0, defaults=values)


def read_case_data(draft: FilingDraft | None) -> dict[str, Any]:
    """Serialize the draft back into the flat case_data blob the browser reads."""

    if draft is None:
        return {}

    data: dict[str, Any] = {}
    _put(data, "jurisdiction", draft.jurisdiction)
    _put(data, "jurisdiction_id", draft.jurisdiction)
    # Old screens still branch on yes/no. The durable value is normalized now;
    # remove this translation when the last legacy screen is retired.
    _put(data, "existing_case", legacy_existing_case_value(draft.existing_case))
    _put(data, "court", draft.court_code)
    _put(data, "court_name", draft.court_name)
    _put(data, "case_category", draft.case_category_code)
    _put(data, "case_category_name", draft.case_category_name)
    _put(data, "case_type", draft.case_type_code)
    _put(data, "case_type_name", draft.case_type_name)
    _put(data, "case_subtype", draft.case_subtype_code)
    _put(data, "case_subtype_name", draft.case_subtype_name)
    _put(data, "filing_type", draft.filing_type_code)
    _put(data, "filing_type_id", draft.filing_type_code)
    _put(data, "filing_type_name", draft.filing_type_name)
    _put(data, "document_type", draft.document_type_code)
    _put(data, "document_type_name", draft.document_type_name)
    _put(data, "previous_case_id", draft.previous_case_id)
    _put(data, "docket_number", draft.docket_number)
    _put(data, "case_title", draft.case_title)
    _put(data, "notice_email", draft.notice_email)
    _put(data, "selected_payment_account", draft.selected_payment_account_id)
    _put(data, "selected_payment_account_name", draft.selected_payment_account_name)
    _put(data, "optional_services", list(draft.optional_services or []))
    _put(data, "amount_in_controversy", draft.amount_in_controversy)
    _put(data, "reason_for_name_change", draft.name_change_reason)
    _put(data, "reason_for_change", draft.name_change_reason)

    for party in FilingParty.objects.filter(draft=draft):
        if party.role == "filer":
            _put(data, "petitioner_first_name", party.first_name)
            _put(data, "petitioner_last_name", party.last_name)
            _put(data, "petitioner_address", party.address_line_1)
            _put(data, "petitioner_email", party.email)
            _put(data, "petitioner_phone", party.phone)
            if party.party_type:
                for key in _PETITIONER_PARTY_TYPE_KEYS:
                    _put(data, key, party.party_type)
            continue
        spec = _PARTY_SPECS.get(party.role)
        if spec is None:
            continue
        for model_field, wire_keys in spec.items():
            for wire_key in wire_keys:
                _put(data, wire_key, getattr(party, model_field))
        if party.role == "petitioner" and party.party_type:
            for key in _PETITIONER_PARTY_TYPE_KEYS:
                _put(data, key, party.party_type)

    filing_parties = [
        {
            "id": party.pk,
            "external_party_id": party.external_party_id,
            "source": party.source,
            "source_case_id": party.source_case_id,
            "representation": party.representation,
            "is_self": party.is_self,
            "role": party.role,
            # Whether the filing is made on behalf of this party -- the filer
            # themselves when they are one, someone they are filing for when
            # they are not. The payload names these as Tyler's filing parties.
            "is_filing_party": party.is_filing_party,
            "party_type": party.party_type,
            "party_type_name": party.party_type_name,
            "first_name": party.first_name,
            "middle_name": party.middle_name,
            "last_name": party.last_name,
            "suffix": party.suffix,
            "organization_name": party.organization_name,
            "email": party.email,
            "phone": party.phone,
            "address_line_1": party.address_line_1,
            "address_line_2": party.address_line_2,
            "city": party.city,
            "state": party.state,
            "zip_code": party.zip_code,
            "country": party.country,
        }
        for party in FilingParty.objects.filter(draft=draft)
    ]
    _put(data, "filing_parties", filing_parties)

    # Supplemental answers are emitted as stored (a False/0 answer is meaningful).
    for key, value in (draft.supplemental_fields or {}).items():
        data[key] = value

    from efile.services.appeals import LOWER_COURT_FIELDS, is_new_appeal

    if any(key in data for key in LOWER_COURT_FIELDS) and not is_new_appeal(draft):
        for key in LOWER_COURT_FIELDS:
            data.pop(key, None)
    return data


def _lead_config(data: dict[str, Any]) -> dict[str, Any]:
    # An empty list here means the filer cleared every service, which is an
    # answer and not an absence -- so this is keyed on presence, not truth. The
    # bare `optional_services` key is only a fallback for older upload blobs:
    # the case-level list lives under that same name, and reaching for it when
    # the lead-specific key is present would fill the lead document from the
    # wrong list entirely.
    lead_services = data.get("lead_optional_services", _MISSING)
    if lead_services is _MISSING:
        lead_services = data.get("optional_services", _MISSING)

    config = {
        "filing_type": data.get("lead_filing_type"),
        "filing_type_name": data.get("lead_filing_type_name"),
        "document_type": data.get("lead_document_type"),
        "document_type_name": data.get("lead_document_type_name"),
        "filing_component": data.get("lead_filing_component"),
        "filing_component_name": data.get("lead_filing_component_name"),
        "cc_email": data.get("lead_cc_email"),
        "optional_services": lead_services,
    }
    return {key: value for key, value in config.items() if value is not _MISSING and value not in (None, "")}


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _apply_document(doc: FilingDocument, file_obj: dict[str, Any], config: dict[str, Any]) -> None:
    if "s3_key" in file_obj and _as_str(file_obj.get("s3_key")) != doc.s3_key:
        doc.original_filename = ""
        doc.original_s3_key = ""
        doc.preparation = ""
        doc.preparation_reviewed_at = None
    if "name" in file_obj:
        doc.name = _as_str(file_obj.get("name"))
        if not doc.original_filename:
            doc.original_filename = doc.name
    if "url" in file_obj:
        doc.public_url = _as_str(file_obj.get("url"))
    if "s3_key" in file_obj:
        doc.s3_key = _as_str(file_obj.get("s3_key"))
    if "type" in file_obj or "content_type" in file_obj:
        doc.content_type = _as_str(file_obj.get("type") or file_obj.get("content_type"))
    if "size" in file_obj:
        doc.size = _positive_int(file_obj.get("size"))

    config_fields = {
        "filing_type_code": "filing_type",
        "filing_type_name": "filing_type_name",
        "document_type_code": "document_type",
        "document_type_name": "document_type_name",
        "filing_component_code": "filing_component",
        "filing_component_name": "filing_component_name",
        "courtesy_copy_email": "cc_email",
    }
    for model_field, config_key in config_fields.items():
        if config_key in config:
            value = config.get(config_key)
            if isinstance(value, dict):
                value = value.get("id") or value.get("code") or ""
            if model_field in {"filing_type_code", "document_type_code"} and getattr(doc, model_field) != _as_str(
                value
            ):
                doc.document_type_confirmed = False
            setattr(doc, model_field, _as_str(value))

    if "optional_services" in config:
        services = config.get("optional_services") or []
        doc.requested_optional_services = [str(c)[:100] for c in services if c]
    elif "requested_optional_services" in config:
        services = config.get("requested_optional_services") or []
        doc.requested_optional_services = [str(c)[:100] for c in services if c]
    elif "optional_services" in file_obj:
        services = file_obj.get("optional_services") or []
        doc.requested_optional_services = [str(c)[:100] for c in services if c]

    # Older upload clients stored the selected component on the file object as
    # {id, name}, while the durable draft config was empty. Preserve that code
    # so the payment payload never falls back to the literal "supporting".
    if not doc.filing_component_code and file_obj.get("filing_component"):
        value = file_obj["filing_component"]
        if isinstance(value, dict):
            value = value.get("id") or value.get("code") or ""
        doc.filing_component_code = _as_str(value)


def _upsert_document(
    draft: FilingDraft,
    role: str,
    sort_order: int,
    file_obj: dict[str, Any],
    config: dict[str, Any],
) -> None:
    doc, _created = FilingDocument.objects.get_or_create(draft=draft, role=role, sort_order=sort_order)
    old_keys = [doc.s3_key, doc.original_s3_key]
    _apply_document(doc, file_obj, config)
    doc.save()
    if old_keys[0] != doc.s3_key:
        transaction.on_commit(lambda: cleanup_unreferenced_uploads(old_keys), robust=True)


@transaction.atomic
def write_upload_data(
    draft: FilingDraft,
    upload_data: dict[str, Any] | None,
    *,
    current_step: WorkflowStepKey | str | None = None,
) -> FilingDraft:
    """Persist a (possibly partial) upload_data blob into FilingDocument rows."""

    locked = FilingDraft.objects.select_for_update().get(pk=draft.pk)
    if locked.status not in ACTIVE_DRAFT_STATUSES:
        raise ValueError("This filing is no longer available to edit.")
    data = dict(upload_data or {})
    update_fields: list[str] = []

    if "guesses" in data:
        guesses = data.get("guesses") or {}
        if draft.extracted_guesses != guesses:
            draft.extracted_guesses = guesses
            update_fields.append("extracted_guesses")

    files = data.get("files") or {}
    lead_config = _lead_config(data)

    if "lead" in files:
        _upsert_document(draft, FilingDocument.Role.LEAD, 0, files.get("lead") or {}, lead_config)
    elif lead_config:
        lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
        if lead is not None:
            _apply_document(lead, {}, lead_config)
            lead.save()

    if "supporting" in files:
        supporting_files = files.get("supporting") or []
        supporting_configs = data.get("supporting_documents") or []
        # Supporting rows are rebuilt from the blob, so anything the browser
        # does not send would be lost. A document's answer to a checklist item
        # is one such thing, and it belongs to the file rather than to the row
        # that happens to describe it, so it is carried across by storage key.
        previous = [
            document
            for document in FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.SUPPORTING)
            if document.s3_key
        ]
        claimed_items = {document.s3_key: document.checklist_item_id for document in previous}
        confirmed_types = {
            document.s3_key: (document.filing_type_code, document.document_type_code)
            for document in previous
            if document.document_type_confirmed
        }
        # Handoff provenance and pending corrections are keyed by row id, so
        # they follow the file to its rebuilt row the same way.
        previous_ids = {document.s3_key: document.pk for document in previous}
        preparation_metadata = {
            document.s3_key: {
                field: getattr(document, field)
                for field in ("original_filename", "original_s3_key", "preparation", "preparation_reviewed_at")
            }
            for document in previous
        }
        FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.SUPPORTING).delete()
        for index, file_obj in enumerate(supporting_files):
            config = supporting_configs[index] if index < len(supporting_configs) else {}
            _upsert_document(draft, FilingDocument.Role.SUPPORTING, index, file_obj or {}, config or {})
        moved = {}
        for document in FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.SUPPORTING):
            if confirmed_types.get(document.s3_key) == (document.filing_type_code, document.document_type_code):
                document.document_type_confirmed = True
                document.save(update_fields=["document_type_confirmed", "updated_at"])
            if metadata := preparation_metadata.get(document.s3_key):
                for field, value in metadata.items():
                    setattr(document, field, value)
                document.save(update_fields=[*metadata, "updated_at"])
            item_id = claimed_items.get(document.s3_key, "")
            if item_id:
                document.checklist_item_id = item_id
                document.save(update_fields=["checklist_item_id", "updated_at"])
            if document.s3_key in previous_ids:
                moved[previous_ids[document.s3_key]] = document.pk
        old_keys = [key for document in previous for key in (document.s3_key, document.original_s3_key)]
        transaction.on_commit(lambda: cleanup_unreferenced_uploads(old_keys), robust=True)
        if moved:
            from efile.services.handoff import carry_document_paths

            carry_document_paths(draft, moved)

    if current_step is not None and draft.current_step != str(current_step):
        draft.current_step = str(current_step)
        update_fields.append("current_step")

    if update_fields:
        draft.save(update_fields=sorted({*update_fields, "updated_at"}))
    return draft


def _document_file(doc: FilingDocument, urls: dict[int, str] | None = None) -> dict[str, Any]:
    file_obj: dict[str, Any] = {"document_id": doc.pk}
    _put(file_obj, "name", doc.name)
    _put(file_obj, "url", (urls or {}).get(doc.pk, doc.public_url))
    _put(file_obj, "s3_key", doc.s3_key)
    _put(file_obj, "type", doc.content_type)
    _put(file_obj, "size", doc.size)
    return file_obj


def _document_config(doc: FilingDocument) -> dict[str, Any]:
    config: dict[str, Any] = {}
    _put(config, "filing_type", doc.filing_type_code)
    _put(config, "filing_type_name", doc.filing_type_name)
    _put(config, "document_type", doc.document_type_code)
    _put(config, "document_type_name", doc.document_type_name)
    _put(config, "filing_component", doc.filing_component_code)
    _put(config, "filing_component_name", doc.filing_component_name)
    _put(config, "cc_email", doc.courtesy_copy_email)
    _put(config, "optional_services", list(doc.requested_optional_services or []))
    _put(config, "requested_optional_services", list(doc.requested_optional_services or []))
    return config


def read_upload_data(draft: FilingDraft | None) -> dict[str, Any]:
    """Reconstruct the upload_data blob the browser reads from document rows."""

    if draft is None:
        return {}

    lead = FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.LEAD).first()
    supporting = list(
        FilingDocument.objects.filter(draft=draft, role=FilingDocument.Role.SUPPORTING).order_by("sort_order", "pk")
    )

    # Imported PDFs must remain usable after the handoff link's original
    # signature expires. Regenerate access URLs from durable private S3 keys.
    from efile.services.handoff import receipt_for
    from efile.utils.s3_upload_handler import S3UploadHandler

    urls = {}
    if draft.correction_of_id or receipt_for(draft):
        from botocore.exceptions import BotoCoreError, ClientError

        handler = S3UploadHandler()
        # Never fall back to an expired source URL on a correction. Missing
        # storage remains visible and is explained by correction readiness.
        if draft.correction_of_id:
            urls = {doc.pk: "" for doc in [lead, *supporting] if doc}
        try:
            if handler._ensure_initialized():
                urls = {doc.pk: handler.get_public_url(doc.s3_key) for doc in [lead, *supporting] if doc and doc.s3_key}
        except (BotoCoreError, ClientError, RuntimeError):
            if not draft.correction_of_id:
                raise
    files: dict[str, Any] = {}
    if lead is not None:
        files["lead"] = _document_file(lead, urls)
    supporting_files = [_document_file(doc, urls) for doc in supporting]
    if supporting_files:
        files["supporting"] = supporting_files

    data: dict[str, Any] = {}
    if files:
        data["files"] = files
    _put(data, "guesses", draft.extracted_guesses)

    if lead is not None:
        _put(data, "lead_filing_type", lead.filing_type_code)
        _put(data, "lead_filing_type_name", lead.filing_type_name)
        _put(data, "lead_document_type", lead.document_type_code)
        _put(data, "lead_document_type_name", lead.document_type_name)
        _put(data, "lead_filing_component", lead.filing_component_code)
        _put(data, "lead_filing_component_name", lead.filing_component_name)
        _put(data, "lead_cc_email", lead.courtesy_copy_email)
        _put(data, "lead_optional_services", list(lead.requested_optional_services or []))

    supporting_configs = [_document_config(doc) for doc in supporting]
    if any(supporting_configs):
        data["supporting_documents"] = supporting_configs

    return data


def draft_snapshot(draft: FilingDraft | None) -> dict[str, Any] | None:
    """Return the stable, JSON-safe representation exposed to the UI."""

    if draft is None:
        return None
    return {
        "id": draft.pk,
        "jurisdiction": draft.jurisdiction,
        "status": draft.status,
        "current_step": draft.current_step,
        "workflow_version": draft.workflow_version,
        "existing_case": draft.existing_case,
        "court_code": draft.court_code,
        "court_name": draft.court_name,
        "case_category_code": draft.case_category_code,
        "case_category_name": draft.case_category_name,
        "case_type_code": draft.case_type_code,
        "case_type_name": draft.case_type_name,
        "case_subtype_code": draft.case_subtype_code,
        "case_subtype_name": draft.case_subtype_name,
        "filing_type_code": draft.filing_type_code,
        "filing_type_name": draft.filing_type_name,
        "document_type_code": draft.document_type_code,
        "document_type_name": draft.document_type_name,
        "previous_case_id": draft.previous_case_id,
        "docket_number": draft.docket_number,
        "case_title": draft.case_title,
        "selected_payment_account_id": draft.selected_payment_account_id,
        "selected_payment_account_name": draft.selected_payment_account_name,
        "optional_services": draft.optional_services,
        "extracted_guesses": draft.extracted_guesses,
        "document_checklist_acknowledged": draft.document_checklist_acknowledged,
        "supplemental_fields": draft.supplemental_fields,
        "document_count": FilingDocument.objects.filter(draft=draft).count(),
        "party_count": FilingParty.objects.filter(draft=draft).count(),
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
        "submitted_at": draft.submitted_at.isoformat() if draft.submitted_at else None,
    }
