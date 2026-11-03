"""Account-free guidance, never recorded or accepted as a live fee quote."""

import re
from decimal import Decimal, InvalidOperation

from efile.services.fee_surcharges import PLATFORM_FEE_LABEL, matching_exemption, surcharge_estimate
from efile.services.filing_plans import _codes
from efile.utils.config_loader import config_loader


def money(value):
    try:
        amount = Decimal(str(value).replace("$", "").replace(",", "").strip())
        return amount if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def contingent_fee(draft, document, rules):
    """First matching rule; thresholds are inclusive and ordered in YAML."""
    for rule in rules:
        if rule.get("initial_only") and (draft.existing_case != "new" or document.role != "lead"):
            continue
        if not re.search(rule["case_pattern"], draft.case_type_name, re.I):
            continue
        if not re.search(rule["filing_pattern"], document.filing_type_name, re.I):
            continue
        amount = money(draft.amount_in_controversy)
        bands = rule["bands"]
        if amount is not None:
            bands = next(([b] for b in bands if b.get("up_to") is None or amount <= Decimal(str(b["up_to"]))), [])
        if not bands:
            return None
        return min(Decimal(str(b["low"])) for b in bands), max(Decimal(str(b["high"])) for b in bands)
    return None


AMOUNT_FIELDS = ("amountincontroversy", "civilclaimamount", "probateestateamount")


def amount_dependent(document, option):
    """A zero or blank code fee may be a placeholder for a calculated fee."""
    return getattr(document, "filing_requires_amount_in_controversy", False) or any(
        str(option.get(field, "")).strip().casefold() not in {"", "not available"} for field in AMOUNT_FIELDS
    )


def amount_range(draft, document, option, rules):
    # Our configured bands price amount_in_controversy, not estate values or
    # other monetary inputs. Do not apply the wrong schedule to those inputs.
    if any(
        str(option.get(field, "")).strip().casefold() not in {"", "not available"}
        for field in ("civilclaimamount", "probateestateamount")
    ):
        return None
    return contingent_fee(draft, document, rules)


def estimate_fees(draft, *, first_use=None, exemption=None):
    config = (config_loader.load_jurisdiction_config(draft.jurisdiction) or {}).get("fee_estimates", {})
    waiver_exemption = matching_exemption(config.get("surcharges", {}), draft, exemption) or {}
    documents = list(draft.documents.all())
    options = _codes(
        draft.jurisdiction,
        f"{draft.court_code}/filing_types/",
        initial="false" if draft.existing_case == "existing" else "true",
        category_id=draft.case_category_code,
        type_id=draft.case_type_code,
    )
    by_code = {str(option.get("code")): option for option in options}
    rows = []
    low = high = Decimal("0")
    unknown = False
    used_range = False

    def add_row(label, bounds, note=""):
        nonlocal low, high, unknown
        if bounds is None:
            unknown = True
            rows.append({"label": label, "amount": "Not available yet", "note": note})
        else:
            minimum, maximum = bounds
            low += minimum
            high += maximum
            rows.append({"label": label, "amount": display_range(minimum, maximum), "note": note})

    # CaseType.fee is the entry fee, not a fee for every document. Read it once
    # for a new case, including an explicitly published zero.
    entry_priced_document = None
    if draft.existing_case != "existing" and documents:
        case_types = _codes(
            draft.jurisdiction,
            f"{draft.court_code}/case_types/",
            category_id=draft.case_category_code,
        )
        case_type = next((item for item in case_types if str(item.get("code")) == str(draft.case_type_code)), {})
        case_fee = money(case_type.get("fee")) if case_type.get("initial") in (True, "true") else None
        lead = next((document for document in documents if document.role == "lead"), None)
        lead_option = by_code.get(lead.filing_type_code, {}) if lead else {}
        variable_entry = lead is not None and amount_dependent(lead, lead_option)
        entry_bounds = (case_fee, case_fee) if case_fee is not None else None
        if case_fee is None or (case_fee == 0 and variable_entry):
            entry_bounds = None
            if lead:
                entry_bounds = amount_range(
                    draft, lead, lead_option, [rule for rule in config.get("rules", []) if rule.get("initial_only")]
                )
                if entry_bounds is not None:
                    used_range = True
                    entry_priced_document = lead
        add_row("Case opening fee", entry_bounds)

    service_cache = {}
    for document in documents:
        option = by_code.get(document.filing_type_code, {})
        fee = money(option.get("fee"))
        variable = amount_dependent(document, option)
        note = ""
        bounds = None
        if document is entry_priced_document and variable:
            # The configured entry range already priced this document.
            bounds = (Decimal("0"), Decimal("0"))
            note = "Included in the case opening estimate."
        elif variable:
            bounds = amount_range(
                draft, document, option, [rule for rule in config.get("rules", []) if not rule.get("initial_only")]
            )
            if bounds is not None:
                used_range = True
            else:
                note = "This fee depends on an amount entered for the filing. We will calculate it before you file."
        elif fee is not None and fee > 0:
            bounds = (fee, fee)
        elif (fee == 0 or option.get("fee") == "") and all(
            str(option.get(field, "")).strip().casefold() == "not available" for field in AMOUNT_FIELDS
        ):
            # In a complete Tyler filing-code row, blank means no separate
            # fixed document fee is listed. It says nothing about case entry.
            bounds = (Decimal("0"), Decimal("0"))
        add_row(document.filing_type_name or document.name or "Document", bounds, note)

        # These are selected per document and submitted in that document's
        # court bundle. Price them before percentage-based processing fees.
        selected_services = getattr(document, "requested_optional_services", None) or []
        if selected_services and document.filing_type_code not in service_cache:
            services = _codes(
                draft.jurisdiction,
                f"{draft.court_code}/filing_types/{document.filing_type_code}/optional_services",
            )
            service_cache[document.filing_type_code] = {
                str(item.get("code") or item.get("id") or ""): item for item in services
            }
        for selected in selected_services:
            code = (
                str(selected.get("code") or selected.get("id") or "") if isinstance(selected, dict) else str(selected)
            )
            service = service_cache[document.filing_type_code].get(code.strip(), {})
            # Unlike the picker normalizer, do not replace missing prices with
            # zero: an unavailable service price leaves the total unknown.
            price = money(service.get("fee") if service.get("fee") not in (None, "") else service.get("cost"))
            quantity = 1
            if str(service.get("multiplier", "")).lower() == "true" and isinstance(selected, dict):
                try:
                    quantity = max(1, int(selected.get("multiplier", 1)))
                except (ValueError, TypeError):
                    quantity = 1  # Matches normalize_optional_services.
            prompted = str(service.get("hasfeeprompt", "")).lower() == "true"
            bounds = (price * quantity, price * quantity) if price is not None and not prompted else None
            label = service.get("name") or service.get("label") or service.get("text") or "Optional service"
            if quantity > 1:
                label = f"{label} × {quantity}"
            add_row(label, bounds)
            if len(documents) > 1:
                rows[-1]["document_name"] = document.name or document.filing_type_name

    notes = [config.get("note", "")] if used_range else []
    if documents:
        policy = config.get("surcharges", {})
        # This is the estimate before a waiver. A waiver account saved from an
        # earlier visit to Review must not price it as already waived.
        method = getattr(draft, "selected_payment_account_type", "")
        if method in policy.get("waiver_account_types", []):
            method = ""
        extra_rows, total_bounds, extra_notes = surcharge_estimate(
            policy,
            draft,
            None if unknown else (low, high),
            first_use=first_use,
            exemption=exemption,
            method=method,
        )
        # A state can say who charges the service fee, and when.
        platform_note = policy.get("platform", {}).get("note", "")
        for label, bounds in extra_rows:
            rows.append(
                {
                    "label": label,
                    "amount": display_range(*bounds) if bounds else "Not available yet",
                    "note": platform_note if label == PLATFORM_FEE_LABEL else "",
                }
            )
        # The payment page presents this guidance once, beside the choice.
        notes.extend(note for note in extra_notes if note != waiver_exemption.get("note"))
        unknown = total_bounds is None
        if total_bounds is not None:
            low, high = total_bounds

    return {
        "waiver_exemption": waiver_exemption,
        "rows": rows,
        "total": display_range(low, high) if rows and not unknown else "",
        "partial": unknown,
        "is_zero": bool(rows) and not unknown and high == 0,
        "note": " ".join(note for note in notes if note),
    }


def display_range(low, high):
    return f"~${low:,.2f}" if low == high else f"${low:,.2f}–${high:,.2f}"
