"""Court bank-payment policy and authenticated account validation.

Read location settings afresh at each enforcement boundary. An absent or
unreadable flag is unknown, never permission to use a bank account.
"""

from urllib.parse import quote

import requests
from django.conf import settings
from django.urls import reverse

from efile.services.draft_urls import draft_url
from efile.services.payment_accounts import account_headers, payment_accounts
from efile.utils.efsp_security import validated_code, validated_jurisdiction

BANK_ACCOUNT_TYPE = "BankAccount"
RESTRICTED_MESSAGE = (
    "This court does not accept bank payments. Choose a credit card or request a fee waiver if you qualify."
)
UNKNOWN_MESSAGE = "We could not confirm whether this court accepts bank payments. Try again, choose a credit card, or request a fee waiver if you qualify."


def bank_payment_policy(jurisdiction, court):
    """Return allowed/restricted/unknown for this court, without caching failures."""
    if not court:
        return "unknown"
    try:
        jurisdiction = validated_jurisdiction(jurisdiction)
        court = validated_code(court)
        response = requests.get(
            f"{settings.EFSP_URL}/jurisdictions/{quote(jurisdiction, safe='')}/codes/courts/{quote(court, safe='')}/codes",
            headers=account_headers(jurisdiction, ""),
            timeout=10,
            allow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        flag = data.get("restrictbankaccountpayment") if isinstance(data, dict) else None
    except (requests.RequestException, ValueError):
        return "unknown"
    if flag is False or (isinstance(flag, str) and flag.strip().lower() == "false"):
        return "allowed"
    if flag is True or (isinstance(flag, str) and flag.strip().lower() == "true"):
        return "restricted"
    return "unknown"


def policy_message(policy):
    return {"restricted": RESTRICTED_MESSAGE, "unknown": UNKNOWN_MESSAGE}.get(policy, "")


def require_account_allowed(account, jurisdiction, court):
    if account.get("paymentAccountTypeCode") == BANK_ACCOUNT_TYPE:
        if message := policy_message(bank_payment_policy(jurisdiction, court)):
            raise ValueError(message)


def validate_filing_payment(jurisdiction, court, token, account_id):
    """Resolve the real account type; browser and saved type labels are not authority."""
    if not token:
        raise ValueError("Your court sign-in has expired. Sign in again from the Fees page.")
    if not account_id:
        raise ValueError("Choose a payment method on the Fees page before continuing.")
    try:
        accounts = payment_accounts(jurisdiction, token)
    except (requests.RequestException, ValueError) as error:
        raise ValueError("We could not check your payment method. Try again on the Fees page.") from error
    account = next((a for a in accounts if str(a["paymentAccountID"]) == account_id), None)
    if account is None:
        raise ValueError("This payment method is no longer available. Choose another on the Fees page.")
    require_account_allowed(account, jurisdiction, court)
    return account


def payment_error(draft, message):
    actions = []
    if draft is not None:
        actions.append(
            {
                "label": "Choose payment method",
                "url": draft_url(reverse("payment", kwargs={"jurisdiction": draft.jurisdiction}), draft.pk),
            }
        )
    return {
        "success": False,
        "error_code": "payment_account_unavailable",
        "error": str(message),
        "error_actions": actions,
    }
