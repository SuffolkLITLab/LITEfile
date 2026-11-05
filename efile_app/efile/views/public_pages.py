"""Public, jurisdiction-aware pages for LITEfile."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from efile.build_info import get_build_info
from efile.utils.config_loader import config_loader
from efile.utils.jurisdiction_stuff import has_jurisdiction_login


def _render_public_page(
    request: HttpRequest, template_name: str, jurisdiction: str | None = None, *, extra_context: dict | None = None
) -> HttpResponse:
    if jurisdiction and jurisdiction not in config_loader.get_available_jurisdictions():
        return redirect("efile_choose_jurisdiction")

    is_logged_in = has_jurisdiction_login(request, jurisdiction) if jurisdiction else False

    return render(
        request,
        template_name,
        {
            "page_jurisdiction": jurisdiction,
            "is_logged_in": is_logged_in,
            **(extra_context or {}),
        },
    )


def about_page(request: HttpRequest, jurisdiction: str | None = None) -> HttpResponse:
    return _render_public_page(
        request, "efile/about.html", jurisdiction, extra_context={"build_info": get_build_info()}
    )


def terms_of_service_page(request: HttpRequest, jurisdiction: str | None = None) -> HttpResponse:
    return _render_public_page(request, "efile/terms_of_service.html", jurisdiction)
