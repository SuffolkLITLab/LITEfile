"""The filer's name and address, as their e-filing account already knows them.

This used to be reachable only through ``/api/auth/profile/``, which the "Your
information" page called from JavaScript after the page had rendered. The filer
therefore watched an empty form appear, waited, and then saw their own name drop
into it -- long enough to wonder why a system they had just signed in to did not
know who they were.

Pulling the fetch into a service lets the view ask for the profile *before* it
renders, so the fields are filled on first paint, while the API endpoint keeps
working unchanged for the pages that still call it.

Two calls are needed because Tyler splits the answer: the firm record carries
the address and phone, and the user record carries the name.
"""

import logging

import requests
from django.conf import settings
from requests.exceptions import RequestException

from efile.api.suffolk_api_views import get_tyler_token
from efile.utils.config_loader import config_loader

logger = logging.getLogger(__name__)

#: Where the per-jurisdiction profile is cached for the rest of the session.
SESSION_KEY = "account_profile"

#: A page render waits on this, so it is deliberately shorter than the timeout
#: the API endpoint uses. A slow court is better answered with an empty form the
#: filer can type into than with a page that never arrives.
RENDER_TIMEOUT = 5


def _headers(request, jurisdiction):
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"{jurisdiction.title()}-eFile-Client/1.0",
        "X-API-Key": getattr(settings, "SUFFOLK_EFILE_API_KEY", "") or "",
    }
    tyler_token = get_tyler_token(request, jurisdiction)
    if tyler_token:
        headers[f"tyler-token-{jurisdiction}"] = tyler_token
    else:
        logger.info("No Tyler token found for state '%s' in Suffolk eFile API request", jurisdiction)
    if getattr(request.user, "tyler_user_id", None):
        headers[f"TYLER-ID-{jurisdiction.upper()}"] = request.user.tyler_user_id
    return headers


def default_state_code(jurisdiction):
    """The two-letter state a blank address field should start out as.

    From the jurisdiction's own config, so a Vermont filer is not offered `IL`.
    """
    config = config_loader.load_jurisdiction_config(jurisdiction) or {}
    return (config.get("state") or {}).get("code") or ""


def fetch_account_profile(request, jurisdiction, timeout=10):
    """Read the signed-in filer's account details from the e-filing service.

    Args:
        request: The current request, for its Tyler token and user.
        jurisdiction (str): Jurisdiction code.
        timeout (int): Seconds to wait on each of the two upstream calls.

    Returns:
        dict | None: Account fields (``first_name``, ``last_name``, ``address``,
        ``address_line2``, ``city``, ``state``, ``zip``, ``phone``, and the raw
        ``firm`` record), or None if the account could not be read.
    """
    if not jurisdiction:
        return None

    headers = _headers(request, jurisdiction)
    base = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}"
    try:
        firm_response = requests.get(
            f"{base}/firmattorneyservice/firm", headers=headers, timeout=timeout, allow_redirects=False
        )
        self_response = requests.get(f"{base}/adminusers/user", headers=headers, timeout=timeout, allow_redirects=False)
    except RequestException:
        logger.exception("Could not reach the e-filing service for the account profile")
        return None

    if firm_response.status_code != 200 or self_response.status_code != 200:
        logger.info(
            "Account profile unavailable: firm=%s user=%s",
            firm_response.status_code,
            self_response.status_code,
        )
        return None

    try:
        firm = firm_response.json()
        user = self_response.json()
    except ValueError:
        logger.exception("The e-filing service returned an unreadable account profile")
        return None

    address = firm.get("address") or {}
    return {
        "first_name": user.get("firstName", ""),
        "last_name": user.get("lastName", ""),
        "address": address.get("addressLine1", ""),
        "address_line2": address.get("addressLine2", ""),
        "city": address.get("city", ""),
        # An address the court has on file wins; otherwise the filer's own
        # state is the better guess, and a wrong guess here is a wrong address
        # on a filing.
        "state": address.get("state") or default_state_code(jurisdiction),
        "zip": address.get("zipCode", ""),
        "phone": firm.get("phoneNumber", ""),
        "firm": firm,
    }


def cached_account_profile(request, jurisdiction, timeout=RENDER_TIMEOUT):
    """:func:`fetch_account_profile`, fetched once per session per jurisdiction.

    An account's address does not change while someone fills out a filing, and
    the alternative is paying for two upstream calls every time the filer steps
    back into "Your information" from review.
    """
    cache = request.session.get(SESSION_KEY) or {}
    if jurisdiction in cache:
        return cache[jurisdiction]

    profile = fetch_account_profile(request, jurisdiction, timeout=timeout)
    if profile is None:
        return None

    # The firm record is only wanted by the API response; keeping it out of the
    # session keeps the session row small.
    cache[jurisdiction] = {key: value for key, value in profile.items() if key != "firm"}
    request.session[SESSION_KEY] = cache
    return cache[jurisdiction]
