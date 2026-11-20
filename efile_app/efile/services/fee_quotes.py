"""Whether the fee quote on a draft still describes the filing it would price.

A quote is asked of the EFSP on Payment and shown again on Review. Anything
the filer changes afterwards -- the court, the case type, a document's filing
type, an optional service -- can change what the court charges, and it can be
changed from any of half a dozen screens. Rather than have each of them
remember to clear the quote, the quote carries a fingerprint of everything it
was priced on, and is only ever treated as current while the draft still
matches it.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction

from efile.models import FilingDocument, FilingDraft, FilingParty

WAIVER_ACCOUNT_TYPE = "WV"


class FeeQuoteState:
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"
    WAIVED = "waived"


def fee_inputs(draft: FilingDraft, *, payment_account_id: str | None = None) -> dict[str, Any]:
    """Everything on the draft that the EFSP's fee calculation reads.

    Deliberately generous: a field that turns out not to affect the fee costs
    one extra fee request after it changes, while one left out lets a stale
    total through. `payment_account_id` is the account the quote was asked
    for, which on Payment is chosen before it is saved to the draft.
    """

    documents = [
        {
            "role": document.role,
            "filing_type": document.filing_type_code,
            "document_type": document.document_type_code,
            "component": document.filing_component_code,
            "document_id": document.pk,
            "attached_to": document.attached_to_id,
            "optional_services": sorted(
                json.dumps(service, sort_keys=True, default=str)
                for service in (document.requested_optional_services or [])
            ),
            "amount_in_controversy_required": document.filing_requires_amount_in_controversy,
        }
        for document in FilingDocument.objects.filter(draft=draft).order_by("role", "sort_order", "pk")
    ]
    parties = sorted(
        (
            party.role,
            party.party_type,
            bool(party.is_filing_party),
            party.external_party_id,
            party.source_case_id,
            json.dumps(party.representation, sort_keys=True),
            party.first_name,
            party.middle_name,
            party.last_name,
            party.suffix,
            party.organization_name,
        )
        for party in FilingParty.objects.filter(draft=draft)
    )
    return {
        "jurisdiction": draft.jurisdiction,
        "court": draft.court_code,
        "case_category": draft.case_category_code,
        "case_type": draft.case_type_code,
        "existing_case": draft.existing_case,
        "previous_case_id": draft.previous_case_id,
        "amount_in_controversy": draft.amount_in_controversy,
        "lower_court": {
            key: value for key, value in (draft.supplemental_fields or {}).items() if key.startswith("lower_court_")
        },
        "optional_services": json.dumps(draft.optional_services or [], sort_keys=True, default=str),
        "electronic_service": (draft.supplemental_fields or {}).get("_electronic_service", {}),
        "payment_account": draft.selected_payment_account_id if payment_account_id is None else payment_account_id,
        "documents": documents,
        "parties": parties,
    }


def fee_fingerprint(draft: FilingDraft, *, payment_account_id: str | None = None) -> str:
    encoded = json.dumps(fee_inputs(draft, payment_account_id=payment_account_id), sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fee_inputs_token(draft: FilingDraft) -> str:
    """The draft's fee inputs as a page saw them, apart from the account.

    Payment and Review build the fee request from the draft as it was when the
    page was drawn, and send this back with it. A quote is only recorded while
    the draft still gives the same token -- before the EFSP is asked, and again
    once it answers -- so a total priced on an older version of the filing is
    never stored as current. The account is left out because Payment asks for
    an account the filer has not saved yet; it is bound when the quote is
    recorded.
    """

    return fee_fingerprint(draft, payment_account_id="")


def fee_quote_state(draft: FilingDraft) -> str:
    if draft.selected_payment_account_type == WAIVER_ACCOUNT_TYPE:
        return FeeQuoteState.WAIVED
    if not draft.quoted_fee_total:
        return FeeQuoteState.MISSING
    # A quote saved before fingerprints existed says nothing about what it
    # priced, so it is treated like one that no longer matches.
    if not draft.quoted_fee_fingerprint or draft.quoted_fee_fingerprint != fee_fingerprint(draft):
        return FeeQuoteState.STALE
    return FeeQuoteState.CURRENT


def fee_quote_is_usable(draft: FilingDraft) -> bool:
    """Whether the filing may be submitted against the quote the filer saw."""

    return fee_quote_state(draft) in {FeeQuoteState.CURRENT, FeeQuoteState.WAIVED}


FEE_QUOTE_FIELDS = ("quoted_fee_total", "quoted_fee_breakdown", "quoted_fee_fingerprint")


def invalidate_fee_quote(draft: FilingDraft, *, save: bool = True) -> None:
    """Forget the quote outright, for changes that make it meaningless."""

    draft.quoted_fee_total = ""
    draft.quoted_fee_breakdown = []
    draft.quoted_fee_fingerprint = ""
    if save:
        draft.save(update_fields=[*FEE_QUOTE_FIELDS, "updated_at"])


def quote_from_efsp_response(response: dict[str, Any]) -> tuple[str, list[dict[str, str]]] | None:
    """The total and itemized charges from an EFSP fee response, or None.

    None when the response names no total: a quote that cannot be read is not
    one to record, and Review then says the fee could not be determined.
    """

    if not isinstance(response, dict):
        return None
    total = (response.get("feesCalculationAmount") or {}).get("value")
    if total in (None, ""):
        return None
    try:
        total = str(Decimal(str(total)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return None
    breakdown = []
    for fee in response.get("allowanceCharge") or []:
        if not isinstance(fee, dict) or not (fee.get("chargeIndicator") or {}).get("value"):
            continue
        breakdown.append(
            {
                "label": str((fee.get("allowanceChargeReason") or {}).get("value") or "Court fee"),
                "amount": str((fee.get("amount") or {}).get("value") or "0.00"),
            }
        )
    return total, breakdown


def record_fee_quote(
    draft: FilingDraft,
    total: str,
    breakdown: list[dict[str, str]],
    *,
    payment_account_id: str | None = None,
    inputs_token: str | None = None,
) -> bool:
    """Store the quote with what it priced; False, storing nothing, if that is gone.

    With `inputs_token`, the quote is only stored while the draft (read fresh,
    under a row lock) still gives that token -- see fee_inputs_token.
    """

    with transaction.atomic():
        FilingDraft.objects.select_for_update().filter(pk=draft.pk).first()
        draft.refresh_from_db()
        if inputs_token is not None and fee_inputs_token(draft) != inputs_token:
            return False
        _store_quote(draft, total, breakdown, payment_account_id)
    return True


def _store_quote(draft, total, breakdown, payment_account_id):
    draft.quoted_fee_total = total
    draft.quoted_fee_breakdown = breakdown
    draft.quoted_fee_fingerprint = fee_fingerprint(draft, payment_account_id=payment_account_id)
    draft.save(update_fields=[*FEE_QUOTE_FIELDS, "updated_at"])


def fee_quote_summary(draft: FilingDraft) -> dict[str, Any]:
    """What Review and the fee API tell the page about the quote."""

    state = fee_quote_state(draft)
    current = state == FeeQuoteState.CURRENT
    return {
        "state": state,
        "total": draft.quoted_fee_total if current else "",
        "is_zero": current and Decimal(draft.quoted_fee_total) == 0,
        "breakdown": list(draft.quoted_fee_breakdown or []) if current else [],
    }
