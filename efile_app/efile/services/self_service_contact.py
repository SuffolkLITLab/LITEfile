"""Register the authenticated filer for future service, without serving documents."""

from efile.services.service_contacts import ServiceContactError, get_or_create_contact, proxy_request
from efile.utils.efsp_security import validated_code


def case_id(draft):
    return (draft.submission_response or {}).get("caseId") or draft.previous_case_id


def register_contact(draft, token, first, last):
    """Attach to the returned case ID; Tyler treats repeat attachments as success."""
    case = case_id(draft)
    if not case:
        raise ServiceContactError(
            "This filing has no case ID available for registration. Use the court’s e-filing system to manage your service contacts."
        )
    case = validated_code(str(case))
    contact_id = get_or_create_contact(
        draft,
        token,
        {
            "contact_first_name": first,
            "contact_last_name": last,
            "contact_email": draft.user.email,
        },
    )
    contact_id = validated_code(contact_id)
    try:
        proxy_request(
            draft,
            token,
            f"firmattorneyservice/service-contacts/{contact_id}/cases",
            method="PUT",
            body={"caseId": case},
        )
    except ServiceContactError as error:
        raise ServiceContactError(
            "Your filing was already submitted, but we could not confirm your service registration. "
            "The case may not be ready yet. Try registration again later; do not submit your filing again."
        ) from error
