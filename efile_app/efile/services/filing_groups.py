"""Build one proxy filing per lead PDF, with its explicitly attached PDFs.

The browser still describes one row per stored document. Only the server may
build nested elements, using the draft's relationships and signed storage URLs.
"""

from collections import Counter

from django.conf import settings

from efile.services.efsp_errors import DOCUMENT_ROLE_GUIDANCE
from efile.services.efsp_payload import PayloadValidationError, _efsp_flag
from efile.utils.efsp_security import validated_code

ATTACHMENT_FIELDS = (
    "proxy_enabled",
    "data_url",
    "filename",
    "document_type",
    "filing_component",
    "document_description",
)


def validate_relationships(rows):
    """Validate a complete set of document selections, without trusting labels."""
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        parent_id = row.get("attached_to")
        if parent_id is None:
            continue
        if type(parent_id) is not int or parent_id not in by_id or parent_id == row["id"]:
            raise ValueError("Choose a lead document from this filing for each attachment.")
        parent = by_id[parent_id]
        if parent.get("attached_to") is not None:
            raise ValueError("An attachment must belong to a lead document, not another attachment.")
        if row.get("filing_type") != parent.get("filing_type"):
            raise ValueError("An attachment must use the same filing code as its lead document.")
        if row.get("requested_optional_services") or row.get("courtesy_copy_email"):
            raise ValueError("Choose optional services and email copies on the lead document, not its attachments.")


def group_filing_documents(payload, draft, *, lookups):
    documents = list(draft.documents.order_by("role", "sort_order", "pk"))
    rows = [
        {
            "id": doc.pk,
            "attached_to": doc.attached_to_id,
            "filing_type": doc.filing_type_code,
            "requested_optional_services": doc.requested_optional_services,
            "courtesy_copy_email": doc.courtesy_copy_email,
        }
        for doc in documents
    ]
    try:
        validate_relationships(rows)
    except ValueError as error:
        raise PayloadValidationError(f"{error} Go back to Organize documents.") from error
    bundles = payload["al_court_bundle"]
    if len(bundles) != len(documents):
        raise PayloadValidationError("The documents changed. Reload Review before continuing.")
    by_id = {doc.pk: bundle for doc, bundle in zip(documents, bundles, strict=True)}
    groups = {doc.pk: [by_id[doc.pk]] for doc in documents if doc.attached_to_id is None}
    for doc in documents:
        if doc.attached_to_id is not None:
            groups[doc.attached_to_id].append(by_id[doc.pk])

    result = []
    for lead_id, members in groups.items():
        lead = by_id[lead_id]
        try:
            filing_type = validated_code(lead["filing_type"])
        except ValueError as error:
            raise PayloadValidationError("Choose a valid filing code in Organize documents.") from error
        url = (
            f"{settings.EFSP_URL}/jurisdictions/{draft.jurisdiction}/codes/courts/{draft.court_code}/"
            f"filing_types/{filing_type}/filing_components"
        )
        components = lookups.get(url)
        if not isinstance(components, list):
            if len(members) > 1:
                raise PayloadValidationError("We could not check the court's attachment rules. Try again.")
        else:
            _validate_components(lead, members, components)
        if len(members) > 1:
            # Like EFSPIntegration: the lead PDF is itself the first element.
            # With elements present the proxy ignores a parent's data_url.
            grouped = {key: value for key, value in lead.items() if key not in ATTACHMENT_FIELDS}
            grouped.update(
                proxy_enabled=True,
                tyler_merge_attachments=False,
                elements=[{key: member.get(key) for key in ATTACHMENT_FIELDS} for member in members],
            )
            result.append(grouped)
        else:
            result.append(lead)
    payload["al_court_bundle"] = result


def _validate_components(lead, members, components):
    choices = {str(item["code"]): item for item in components if isinstance(item, dict) and "code" in item}
    if not choices:
        if len(members) > 1:
            raise PayloadValidationError("This filing code does not offer attachments. Choose another filing code.")
        return
    counts = Counter(str(member.get("filing_component") or "") for member in members)
    for code, count in counts.items():
        if code not in choices:
            raise PayloadValidationError("Choose a court document component in Organize documents.")
        if count > 1 and not _efsp_flag(choices[code].get("allowmultiple")):
            raise PayloadValidationError(
                "This filing has too many documents of the same component type. Check Organize documents."
            )
    lead_choice = choices[str(lead.get("filing_component") or "")]
    if str(lead_choice.get("efspcode", "")).upper() == "ATTACH":
        raise PayloadValidationError(
            f"{lead.get('filename', 'This PDF')} is marked as an attachment but has no lead document. "
            + DOCUMENT_ROLE_GUIDANCE,
            problems=[{"name": "al_court_bundle", "code": "document_role_invalid"}],
        )
    missing = [
        item.get("name", code)
        for code, item in choices.items()
        if _efsp_flag(item.get("required")) and not counts[code]
    ]
    if missing:
        raise PayloadValidationError(
            f"{lead.get('filename', 'This filing')} needs {', '.join(missing)}. "
            "If a required PDF has not been uploaded, add it to the draft first. " + DOCUMENT_ROLE_GUIDANCE,
            problems=[{"name": "al_court_bundle", "code": "document_role_invalid"}],
        )
    if any(
        str(choices[str(member.get("filing_component") or "")].get("efspcode", "")).upper() == "LEAD"
        for member in members[1:]
    ):
        raise PayloadValidationError("An attached PDF cannot be another Lead Document. Check Organize documents.")
