"""Configured platform and payment costs for estimates, never live quotes."""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid fee")
    return result


def matching_exemption(policy, draft, exemption=None):
    platform = policy.get("platform", {})
    rules = platform.get("exemptions", [])
    matched = next((r for r in rules if r.get("id") == exemption), None)
    if matched is None:
        matched = next(
            (
                r
                for r in rules
                if r.get("case_pattern")
                and (not r.get("initial_only") or getattr(draft, "existing_case", "") == "new")
                and re.search(r["case_pattern"], getattr(draft, "case_type_name", ""), re.I)
            ),
            None,
        )
    return matched


PLATFORM_FEE_LABEL = "E-filing service fee"


def surcharge_estimate(policy, draft, bounds, *, first_use=None, exemption=None, method=None):
    """Return rows, total bounds and notes, preserving unknowns.

    first_use means this filer/firm has not paid the per-case platform fee.
    exemption is an explicitly established configuration rule ID, never inferred
    from income. Callers without that information leave both arguments unset.
    method overrides the draft's saved payment account type.
    """
    if not policy:
        return [], bounds, []
    notes = []
    if method is None:
        method = getattr(draft, "selected_payment_account_type", "")
    waiver = method in policy.get("waiver_account_types", [])
    if waiver:
        notes.append("A waiver account requests an exemption. The court must confirm your fees.")
    methods = policy.get("payment_methods", {})
    known_method = method in methods
    if not known_method and not waiver:
        notes.append("Processing fees depend on the payment method you choose.")
    platform = policy.get("platform", {})
    matched = matching_exemption(policy, draft, exemption)
    requires_waiver = bool(matched and matched.get("requires_waiver_account"))
    if requires_waiver and not waiver:
        notes.append(matched.get("note", "This exemption requires a waiver payment account."))
    rows = []
    try:
        fee = amount(platform.get("amount", 0))
        timing = platform.get("timing", "new_case")
        if timing not in {"new_case", "first_filing_per_filer"}:
            raise ValueError("Unknown platform timing")
        if timing == "new_case":
            applies = draft.existing_case != "existing"
        else:
            applies = True if draft.existing_case == "new" else first_use
        platform_bounds = (fee, fee) if applies is True else ((ZERO, ZERO) if applies is False else (ZERO, fee))
        if applies is None and fee:
            notes.append(
                "The service fee may apply if this is your first filing in this case with this account or firm."
            )
        if matched and (not requires_waiver or waiver):
            platform_bounds = (ZERO, ZERO)
        if fee:
            rows.append((PLATFORM_FEE_LABEL, platform_bounds))
        if bounds is None:
            rows.append(("Processing fee", None))
            return rows, None, notes
        subtotal = tuple(bounds[i] + platform_bounds[i] for i in (0, 1))
        choices = [methods[method]] if known_method else list(methods.values())
        if not choices:
            raise ValueError("No payment fee rules")
        charges = []
        totals = []
        for choice in choices:
            fixed = amount(choice.get("fixed", 0))
            percent = amount(choice.get("percent", 0)) / 100
            basis = choice.get("basis", "court_and_platform")
            if basis not in {"court", "court_and_platform"}:
                raise ValueError("Unknown percentage basis")
            for i in (0, 1):
                base = subtotal[i] if basis == "court_and_platform" else bounds[i]
                charge = (fixed + base * percent).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if choice.get("only_when_amount_due", True) and subtotal[i] == 0:
                    charge = ZERO
                charges.append(charge)
                totals.append(subtotal[i] + charge)
        rows.append(("Processing fee", (min(charges), max(charges))))
        result = (min(totals), max(totals))
        # Account type alone does not establish court approval.
        if waiver:
            return rows, None, notes
        return rows, result, notes
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return [("Service and processing fees", None)], None, notes
