"""Validate input before constructing credentialed EFSP requests."""

import re
from functools import partial, wraps

from django.http import JsonResponse
from django.views.decorators.cache import never_cache

from efile.utils.config_loader import config_loader
from efile.utils.jurisdiction_stuff import get_jurisdiction_from_request, has_jurisdiction_login


def validated_jurisdiction(value):
    if not isinstance(value, str) or value.lower() not in config_loader.get_available_jurisdictions():
        raise ValueError("Choose a supported jurisdiction.")
    return value.lower()


def validated_code(value):
    """Accept one literal code, including Tyler's colon-separated court codes.

    Reject percent escapes as well as URL delimiters so neither Requests nor
    a downstream router can reinterpret a code as a different path.
    """
    value = str(value or "")
    if value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_:.-]+", value):
        raise ValueError("Invalid court or filing code.")
    return value


def require_efsp_login(view=None, *, session_jurisdiction=False):
    """Private proxy APIs require the active account's jurisdiction and token."""

    if view is None:
        return partial(require_efsp_login, session_jurisdiction=session_jurisdiction)

    @wraps(view)
    @never_cache
    def wrapped(request, *args, **kwargs):
        try:
            jurisdiction = validated_jurisdiction(
                request.session.get("jurisdiction") if session_jurisdiction else get_jurisdiction_from_request(request)
            )
        except ValueError as error:
            return JsonResponse({"success": False, "error": str(error)}, status=400)
        if not has_jurisdiction_login(request, jurisdiction):
            return JsonResponse({"success": False, "error": "Sign in to this jurisdiction first."}, status=401)
        return view(request, *args, **kwargs)

    return wrapped
