"""Subsequent filing choices bound to a confirmed court case."""

import hashlib
import json
from urllib.parse import quote

import requests
from django.conf import settings

from efile.services.existing_cases import import_ready


def case_fingerprint(draft):
    return hashlib.sha256(
        json.dumps(
            [
                draft.court_code,
                draft.previous_case_id,
                draft.case_category_code,
                draft.case_type_code,
                draft.existing_case_snapshot,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def permitted_filing_types(draft):
    if not import_ready(draft):
        raise ValueError("Find and confirm your court case before choosing a type of document (filing code).")
    snapshot = draft.existing_case_snapshot
    if any(snapshot.get(field) != getattr(draft, field) for field in ("case_category_code", "case_type_code")):
        raise ValueError("Your case details changed. Confirm the court case again.")
    if not draft.case_category_code or not draft.case_type_code:
        raise ValueError("The court did not return the case classification. Load the case again.")
    url = f"{settings.EFSP_URL}/jurisdictions/{draft.jurisdiction}/codes/courts/{quote(draft.court_code, safe='')}/filing_types/"
    try:
        response = requests.get(
            url,
            params={
                "initial": "false",
                "category_id": draft.case_category_code,
                "type_id": draft.case_type_code,
            },
            timeout=30,
        )
        response.raise_for_status()
        options = response.json()
        if not isinstance(options, list) or any(
            not isinstance(option, dict) or not option.get("code") or not option.get("name") for option in options
        ):
            raise ValueError
    except (requests.RequestException, ValueError) as exc:
        raise ValueError("We could not check this case's filing codes. Try again.") from exc
    return [{**option, "value": str(option["code"]), "text": str(option["name"])} for option in options]
